# ruff: noqa: SIM105
"""asyncua 2.0.1 against an independent server: the #169 open62541 lab.

The things the python-opcua mock cannot show: browse continuation points
(maxReferencesPerNode=100 over a 1200-child folder), server operation limits,
history continuation points (25 values per page), namespace indexes that move
across a restart (--pad-namespace), namespace-0 structures (#171), and what
asyncua's own watchdogs do when the server stalls.

    OPEN62541_LAB_SERVER=/path/to/lab_server \
    uv run --no-sync --with asyncua==2.0.1 python docs/asyncua-spike/probe_lab.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import free_port, lab_available, make_pki, open62541_lab, report
from asyncua import Client, ua
from opcua_mcp_server import variant_to_json

logging.disable(logging.CRITICAL)
LAB_NS = "urn:opcua-mcp:conformance-lab"


async def browse(client: Client, ns: int) -> None:
    description = ua.BrowseDescription()
    description.NodeId = ua.NodeId("Large", ns)
    description.BrowseDirection = ua.BrowseDirection.Forward
    description.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    description.IncludeSubtypes = True
    description.ResultMask = ua.BrowseResultMask.All
    params = ua.BrowseParameters()
    params.NodesToBrowse = [description]
    params.RequestedMaxReferencesPerNode = 0  # "no limit": the server's own cap applies
    first = (await client.uaclient.browse(params))[0]
    pages, total, cp = 1, len(first.References), first.ContinuationPoint
    while cp:
        nxt = ua.BrowseNextParameters()
        nxt.ContinuationPoints, nxt.ReleaseContinuationPoints = [cp], False
        result = (await client.uaclient.browse_next(nxt))[0]
        total, cp, pages = total + len(result.References), result.ContinuationPoint, pages + 1
    first = (await client.uaclient.browse(params))[0]
    release = ua.BrowseNextParameters()
    release.ContinuationPoints, release.ReleaseContinuationPoints = [first.ContinuationPoint], True
    released = (await client.uaclient.browse_next(release))[0]
    report(
        "supported" if total >= 1200 and pages > 1 else "bug",
        "browse.continuation",
        f"Large folder: {total} references over {pages} Browse/BrowseNext pages "
        f"(server cap 100 per node); release -> {released.StatusCode.name}",
    )


async def operation_limits(client: Client, ns: int) -> None:
    limits = client.get_node(ua.NodeId(ua.ObjectIds.Server_ServerCapabilities_OperationLimits))
    children = {(await c.read_browse_name()).Name: c for c in await limits.get_children()}
    max_read = await children["MaxNodesPerRead"].read_value()
    params = ua.ReadParameters()
    for i in range(max_read + 10):
        rv = ua.ReadValueId()
        rv.NodeId, rv.AttributeId = ua.NodeId(f"Large/Item{i:04d}", ns), ua.AttributeIds.Value
        params.NodesToRead.append(rv)
    try:
        await client.uaclient.read(params)
        outcome = "answered"
    except ua.UaStatusCodeError as error:
        outcome = f"{type(error).__name__} (code {error.code:#x})"
    report(
        "supported",
        "read.operation-limits",
        f"MaxNodesPerRead={max_read}; a Read of {max_read + 10} raises {outcome}: the ServiceFault "
        f"arrives as a typed UaStatusCodeError, which operation_limits.py chunking avoids",
    )


async def history(client: Client, ns: int) -> None:
    node = client.get_node(ua.NodeId("Dynamic/Ramp", ns))
    details = ua.ReadRawModifiedDetails()
    details.IsReadModified = False
    details.EndTime = datetime.now(timezone.utc)
    details.StartTime = details.EndTime - timedelta(seconds=40)
    details.NumValuesPerNode = 0
    details.ReturnBounds = False
    first = await node.history_read(details)
    pages, count, cp = 1, len(first.HistoryData.DataValues), first.ContinuationPoint
    while cp and pages < 50:
        more = await node.history_read(details, cp)
        count, cp, pages = (
            count + len(more.HistoryData.DataValues),
            more.ContinuationPoint,
            pages + 1,
        )
    report(
        "supported" if pages > 1 else "note",
        "history.raw-continuation",
        f"Dynamic/Ramp over 40s, NumValuesPerNode=0: {count} values over {pages} pages "
        f"(server pages at 25)",
    )


async def structures(client: Client, ns: int) -> None:
    rows = []
    for name in ("Range", "EUInformation"):
        dv = await client.get_node(ua.NodeId(f"Structures/{name}", ns)).read_data_value()
        rows.append(
            f"{name}: VariantType={dv.Value.VariantType.name}, Python type "
            f"{type(dv.Value.Value).__name__}, records.variant_to_json -> "
            f"{variant_to_json(dv.Value)!r}"
        )
    report(
        "differs",
        "values.structure-live",
        f"{rows}. asyncua decodes namespace-0 structures into typed dataclasses, so the data is "
        f"there for a Part 6 JSON encoding (#171); records.py still falls back to str()",
    )


async def namespaces(pki: dict) -> None:
    port = free_port()
    with open62541_lab(pki, port=port) as (url, _, _):
        client = Client(url, timeout=8)
        await client.connect()
        before = await client.get_namespace_index(LAB_NS)
    # The server is gone; the same port comes back with an extra namespace first.
    await asyncio.sleep(1.5)
    state = client.uaclient.state.name
    with open62541_lab(pki, pad=True, port=port):
        try:
            await client.get_namespace_index(LAB_NS)
            same_client = "answered"
        except Exception as error:
            same_client = f"{type(error).__name__}: {error}"
        await client.disconnect()
        fresh = Client(url, timeout=8)
        async with fresh:
            after = await fresh.get_namespace_index(LAB_NS)
            stale = await fresh.get_node(ua.NodeId("Scalars/Int32", before)).read_data_value(
                raise_on_bad_status=False
            )
            moved = await fresh.get_node(ua.NodeId("Scalars/Int32", after)).read_data_value()
    report(
        "supported",
        "namespaces.reconnect",
        f"lab namespace index {before} before the restart, {after} after --pad-namespace. The old "
        f"client, with auto_reconnect=False, is {state} and a call raises {same_client}; a fresh "
        f"client's NamespaceArray is read from the server, never cached by asyncua, so the index "
        f"moves (a stale ns={before} read -> {stale.StatusCode.name}; ns={after} -> "
        f"{moved.Value.Value}). The per-connection rebind in connection.py carries over",
    )


async def stall(pki: dict, watchdog: float, seconds: float, subscribe: bool) -> str:
    """SIGSTOP the lab for `seconds`, SIGCONT it, and describe what the client did."""
    with open62541_lab(pki) as (url, _, proc):
        client = Client(url, timeout=8, watchdog_intervall=watchdog)
        lost: list[str] = []

        async def on_lost(exc):
            lost.append(f"{type(exc).__name__} at {time.monotonic() - stopped:.1f}s")

        client.connection_lost_callback = on_lost
        await client.connect()
        sub_ids = []
        if subscribe:
            params = ua.CreateSubscriptionParameters()
            params.RequestedPublishingInterval = 200
            params.RequestedMaxKeepAliveCount = 20
            params.RequestedLifetimeCount = 1000
            params.PublishingEnabled = True

            class Handler:
                def datachange_notification(self, node, value, data):
                    pass

            sub = await client.create_subscription(params, Handler())
            ns = await client.get_namespace_index(LAB_NS)
            await sub.subscribe_data_change(client.get_node(ua.NodeId("Dynamic/Ramp", ns)))
            sub_ids.append(sub.subscription_id)
            await asyncio.sleep(1)
        os.kill(proc.pid, signal.SIGSTOP)
        stopped = time.monotonic()
        await asyncio.sleep(seconds)
        os.kill(proc.pid, signal.SIGCONT)
        await asyncio.sleep(max(3.0, seconds / 2))
        state = client.uaclient.state.name
        try:
            await client.nodes.server_state.read_value()
            read = "a read works"
        except Exception as error:
            read = f"a read raises {type(error).__name__}"
        if subscribe:
            sub_ids.append(sub.subscription_id)
        try:
            await client.disconnect()
        except Exception:
            pass
    subs = f"; subscription id {sub_ids[0]} -> {sub_ids[1]}" if subscribe else ""
    return f"state {state}, {read}, connection_lost_callback {lost or 'never'}{subs}"


async def main() -> None:
    if not lab_available():
        report("skip", "lab", "OPEN62541_LAB_SERVER not set")
        return
    pki = make_pki(Path(tempfile.mkdtemp(prefix="asyncua-spike-pki-")), "server", "client", "user")
    with open62541_lab(pki) as (url, _, _):
        await asyncio.sleep(10)  # history for the ramp to accumulate
        client = Client(url, timeout=8)
        async with client:
            ns = await client.get_namespace_index(LAB_NS)
            for probe in (browse, operation_limits, history, structures):
                try:
                    await probe(client, ns)
                except Exception as error:
                    report("bug", probe.__name__, f"{type(error).__name__}: {error}")
    await namespaces(pki)

    report(
        "differs",
        "keepalive.stall-default",
        f"server SIGSTOPped for 2.5s, default watchdog_intervall=1.0: "
        f"{await stall(pki, 1.0, 2.5, False)}. asyncua's supervisor reads ServerState every "
        f"watchdog_intervall with that as its timeout, and with auto_reconnect=False a single "
        f"missed probe ends the session for good; python-opcua has no such probe (its KeepAlive "
        f"thread only renews the channel), so today a stall fails only the requests made during it",
    )
    report(
        "differs",
        "keepalive.stale-subscription",
        f"server SIGSTOPped for 9s with watchdog_intervall=30 (probe out of the way) and one "
        f"subscription (200ms x 20 keep-alives): {await stall(pki, 30.0, 9.0, True)}. The "
        f"stale-subscription watchdog runs even with auto_reconnect=False and re-creates the "
        f"subscription under a NEW id without telling the caller",
    )


asyncio.run(main())
