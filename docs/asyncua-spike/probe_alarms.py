# ruff: noqa: E501
"""Alarms & Conditions through asyncua 2.0.1, against packages/mock-server-alarms.

ConditionRefresh ordering (RefreshStart, the retained conditions, RefreshEnd)
with a synchronous handler and with an asynchronous one, then every action in
contract/tools.json -> events.actions on the mock's ExclusiveLimitAlarm.

    uv run --no-sync --with asyncua==2.0.1 python docs/asyncua-spike/probe_alarms.py
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random

from _common import alarm_mock, load_json, report
from asyncua import Client, ua
from opcua_mcp_server.events import event_record
from probe_services import event_filter, subscription_parameters

logging.disable(logging.CRITICAL)
EVENTS = load_json("contract/tools.json")["events"]
START, END = EVENTS["refreshStartEventTypeNodeId"], EVENTS["refreshEndEventTypeNodeId"]


class SyncHandler:
    def __init__(self):
        self.records = []

    def event_notification(self, event):
        self.records.append(event_record(event.event_fields))


class AsyncHandler(SyncHandler):
    """An async handler that yields before recording, as an await on I/O would."""

    async def event_notification(self, event):  # type: ignore[override]
        await asyncio.sleep(random.random() / 100)
        self.records.append(event_record(event.event_fields))


async def refresh(client: Client, handler) -> list[str]:
    sub = await client.create_subscription(subscription_parameters(), handler)
    await sub.subscribe_events(
        ua.NodeId(ua.ObjectIds.Server), evfilter=event_filter(), queuesize=EVENTS["subscriptionRequest"]["queueSize"]
    )  # fmt: skip
    await asyncio.sleep(0.5)
    handler.records.clear()
    await client.get_node(EVENTS["conditionTypeNodeId"]).call_method(
        ua.NodeId.from_string(EVENTS["conditionRefreshMethodNodeId"]),
        ua.Variant(sub.subscription_id, ua.VariantType.UInt32),
    )
    await asyncio.sleep(1.0)
    await sub.delete()
    return [
        "Start" if r["event_type"] == START else "End" if r["event_type"] == END else "Cond"
        for r in handler.records
    ]


async def main() -> None:
    with alarm_mock() as url:
        client = Client(url, timeout=10)
        async with client:
            sync_orders = [await refresh(client, SyncHandler()) for _ in range(5)]
            ok = all(o and o[0] == "Start" and o[-1] == "End" and "Cond" in o for o in sync_orders)
            report(
                "supported" if ok else "bug",
                "alarms.refresh-order-sync",
                f"5 ConditionRefreshes, sync handler: {sync_orders}",
            )
            async_orders = [await refresh(client, AsyncHandler()) for _ in range(5)]
            ok = all(o and o[0] == "Start" and o[-1] == "End" for o in async_orders)
            report(
                "supported" if ok else "differs",
                "alarms.refresh-order-async",
                f"5 ConditionRefreshes, async handler that awaits before recording: {async_orders}. "
                f"asyncua 2.0 runs each notification as its own task, so only a sync handler "
                f"keeps arrival order",
            )

            handler = SyncHandler()
            await refresh(client, handler)
            condition = next(r for r in handler.records if r.get("condition_id"))
            report(
                "supported",
                "alarms.condition-record",
                f"refreshed condition record: { {k: condition[k] for k in ('condition_id', 'condition_name', 'active', 'acked', 'retain', 'severity')} }",
            )
            await act(client, condition)


async def current(client: Client) -> dict:
    """The condition as the server reports it now: each action changes its EventId."""
    for _ in range(3):
        handler = SyncHandler()
        order = await refresh(client, handler)
        found = [r for r in handler.records if r.get("condition_id")]
        if found:
            return found[-1]
    raise RuntimeError(f"no retained condition in the refresh: {order}")


async def rearm(client: Client) -> None:
    """Below the limit, then above it: a freshly raised, unacknowledged alarm (as the e2e suite does)."""
    temperature = client.get_node("ns=1;i=1001")
    for value in (20.0, 100.0):
        await temperature.write_value(ua.DataValue(ua.Variant(value, ua.VariantType.Double)))
        await asyncio.sleep(1)


async def act(client: Client, condition: dict) -> None:
    node = client.get_node(condition["condition_id"])
    shelving = None
    for child in await node.get_children():
        if (await child.read_browse_name()).Name == EVENTS["shelvingStateBrowseName"]:
            shelving = child
    actions = {k: v for k, v in EVENTS["actions"].items() if not k.startswith("$")}
    # Part 9 order: acknowledge then confirm on a freshly raised alarm (confirming
    # ends its retention, hence the second rearm), then a comment and the shelving
    # state machine. Every EventId is re-read first, because each state change
    # issues a new one and the server refuses a stale one with BadEventIdUnknown.
    sequence = ["rearm", "acknowledge", "confirm", "rearm", "comment", "unshelve", "shelveFor",
                "unshelve", "shelve"]  # fmt: skip
    outcomes = []
    for name in sequence:
        if name == "rearm":
            await rearm(client)
            continue
        spec = actions[name]
        target = node if spec["on"] == "condition" else shelving
        method = None
        for child in await target.get_children():
            if (await child.read_browse_name()).Name == spec["browseName"]:
                method = child.nodeid
        method = method or ua.NodeId.from_string(spec["methodNodeId"])
        if spec["takes"] == "eventIdAndComment":
            event_id = (await current(client))["event_id"]
            args = [
                ua.Variant(base64.b64decode(event_id), ua.VariantType.ByteString),
                ua.Variant(ua.LocalizedText(f"{name} via asyncua"), ua.VariantType.LocalizedText),
            ]
        elif spec["takes"] == "duration":
            args = [ua.Variant(60_000.0, ua.VariantType.Double)]
        else:
            args = []
        request = ua.CallMethodRequest()
        request.ObjectId, request.MethodId, request.InputArguments = target.nodeid, method, args
        result = (await client.uaclient.call([request]))[0]
        outcomes.append(f"{name}={result.StatusCode.name}")
    after = await current(client)
    report(
        "supported",
        "alarms.actions",
        f"every contract action as a CallMethodRequest on the condition or its ShelvingState: "
        f"{outcomes}; condition afterwards acked={after['acked']}. OneShotShelve is "
        f"BadInternalError on this node-opcua mock for every client (pinned in "
        f"tests/e2e/test_events_e2e.py), so that one is the server's answer, not asyncua's",
    )


asyncio.run(main())
