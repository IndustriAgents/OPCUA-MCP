"""End-to-end tests for the OPC UA MCP servers.

Each test runs against BOTH the Python and the Node MCP server (parameterised via
the ``mcp_session`` fixture), driving them over stdio with the official ``mcp``
client SDK, against the mock industrial OPC UA server.

Run:
    cd tests && uv run pytest -v
    # only one implementation (brackets match the parametrisation id, not
    # test names — plain `-k node` would also match `test_read_opcua_node`):
    cd tests && uv run pytest -v -k "[python]"
    cd tests && uv run pytest -v -k "[node]"
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager

import pytest
from conftest import ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# --- Stable node IDs in the mock server's address space (namespace 2) ----------
# Sensors / actuators / status keep fixed identifiers; method identifiers below
# depend on the method argument nodes and are validated dynamically in the
# call-method test rather than hard-trusted.
NODE = {
    "Temperature": "ns=2;i=3",
    "Pressure": "ns=2;i=4",
    "MotorSpeed": "ns=2;i=10",
    "PumpEnabled": "ns=2;i=12",  # Boolean actuator
    "ValvePosition": "ns=2;i=13",  # Double actuator
    "SystemMode": "ns=2;i=19",
    "ProductionRate": "ns=2;i=21",
    "StartProductionCommand": "ns=2;i=23",  # Double command variable
    "StopProductionCommand": "ns=2;i=24",  # Boolean command variable
    "EmergencyStopCommand": "ns=2;i=25",  # Boolean command variable
    "ResetSystemCommand": "ns=2;i=26",  # Boolean command variable
    "IndustrialControlSystem": "ns=2;i=1",
    "Methods": "ns=2;i=27",
    # The only writable nodes the simulation leaves alone. Every other writable
    # node is an actuator the mock republishes from its own state once a second,
    # so writing one and reading it back races a timer — which is precisely how
    # `test_batch_write_keeps_valid_items_when_one_node_is_rejected` passed here
    # and then failed a release verify. Write-then-read-back belongs on these.
    "ScratchDouble": "ns=2;i=41",
    "ScratchBoolean": "ns=2;i=42",
    # The one node that says what its number *means*: an AnalogItemType with
    # EngineeringUnits (°C), EURange (0 to 150) and InstrumentRange (-50 to 250).
    # Its id is fixed in the mock rather than assigned in sequence, so adding a
    # node cannot renumber it out from under this map.
    "ScratchAnalog": "ns=2;i=90",
}

# A node id the mock does not have, for the failure-path tests.
UNKNOWN_NODE = "ns=2;i=999999"

CORE_TOOLS = {
    "read_opcua_nodes",
    "browse_opcua_nodes",
    "write_opcua_nodes",
    "call_opcua_method",
    # Health/diagnostics: never capability-gated, because ServerStatus is
    # mandatory in OPC UA — every server has one.
    "get_server_status",
    "subscribe_opcua_nodes",
    "list_subscriptions",
    "unsubscribe_opcua_nodes",
    # Events and Alarms & Conditions: not capability-gated either, so a server
    # that raises nothing still advertises them. See e2e/test_events_e2e.py.
    "subscribe_events",
    "read_events",
    "list_active_alarms",
    "acknowledge_alarm",
}

# The resource carrying what the subscriptions have delivered.
SUBSCRIPTIONS_URI = "opcua://subscriptions"

# Raw and aggregate history are one tool; both runtimes expose it under this name.
HISTORY_TOOL = {
    "python": "read_opcua_history",
    "node": "read_opcua_history",
}

NODE_BUILD = ROOT / "packages" / "server-node" / "build" / "index.js"

# ISO-8601 UTC, as `resultShapes.historyRecords` requires: a trailing `Z`, and no
# space-separated `str(datetime)` form. Sub-second digits vary by runtime
# (microseconds from Python, milliseconds from Node), which the contract allows.
ISO_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _server_params(impl: str, url: str) -> StdioServerParameters:
    # The shared E2E suite exercises control operations against a deliberately
    # unsecured in-process fixture. Production defaults are observe-only, so
    # tests must opt in to this lab-only combination explicitly.
    env = {
        **os.environ,
        "OPCUA_SERVER_URL": url,
        "OPCUA_PROFILE": "full",
        "OPCUA_ALLOW_INSECURE_CONTROL": "true",
    }
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    if impl == "node":
        return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)
    raise ValueError(impl)


@pytest.fixture(params=["python", "node"])
def server(request, opcua_server):
    """The ``(impl_name, StdioServerParameters)`` for each server implementation.

    This is a *sync* fixture on purpose: the stdio/anyio client context is opened
    and closed inside each test's own task (via ``connect`` below) so that anyio
    cancel scopes are not entered and exited across different tasks.
    """
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip(
            "Node server not built — run `npm install && npm run build` in packages/server-node"
        )
    return impl, _server_params(impl, opcua_server)


@asynccontextmanager
async def connect(params: StdioServerParameters):
    """Open an initialised MCP ClientSession over stdio."""
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


# --- helpers -------------------------------------------------------------------


def text_of(result) -> str:
    """Concatenate all text content blocks of a CallToolResult."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def records_of(result) -> list[dict]:
    """Parse a history-family response into its records.

    Both servers emit one JSON object per content block, shaped by the contract's
    ``resultShapes.historyRecords``, so this needs no per-implementation branch.
    It used to: the Node server returned a single array of raw node-opcua
    ``DataValue``s (``{"statusCode": {"value": 1}, "sourceTimestamp": …}``) while
    the Python server returned flat records, and a caller had to know which
    server it was talking to (issue #23).
    """
    records = []
    for block in result.content:
        text = getattr(block, "text", None)
        assert text is not None, f"non-text block in a history response: {block!r}"
        try:
            records.append(json.loads(text))
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            raise AssertionError(f"history block is not JSON: {text!r}") from exc
    return records


async def tool_names(session) -> set[str]:
    res = await session.list_tools()
    return {t.name for t in res.tools}


async def wait_for_node_value(
    session, node_id: str, expected: str, attempts: int = 8, delay: float = 1.0
) -> str:
    """Poll a node until its value contains ``expected``.

    The mock server's method callbacks mutate internal state; the OPC UA node
    values are propagated by the 1 Hz simulation loop, so there is up to ~1s of
    lag between calling a method and seeing the node change.
    """
    text = ""
    for _ in range(attempts):
        result = await session.call_tool("read_opcua_nodes", {"node_ids": [node_id]})
        text = text_of(result)
        if expected in text:
            return text
        await asyncio.sleep(delay)
    return text


async def wait_for_changes(
    session, subscription_id: str, at_least: int = 2, attempts: int = 10, delay: float = 1.0
) -> dict:
    """Poll `list_subscriptions` until one subscription has buffered enough changes.

    Polling rather than sleeping a fixed time: how fast the OPC UA server
    publishes is its decision, not ours, and the mock's simulation loop only
    moves the sensors once a second. Returns whatever the last look found, so a
    caller that never reaches `at_least` still gets a record to assert against.
    """
    record = {}
    for _ in range(attempts):
        result = await session.call_tool("list_subscriptions", {})
        for candidate in records_of(result):
            if candidate["subscription_id"] == subscription_id:
                record = candidate
                break
        if record.get("change_count", 0) >= at_least:
            return record
        await asyncio.sleep(delay)
    return record


# --- tests ---------------------------------------------------------------------


async def test_lists_core_tools(server):
    impl, params = server
    async with connect(params) as session:
        names = await tool_names(session)
    assert names >= CORE_TOOLS, f"{impl}: missing core tools: {CORE_TOOLS - names}"


async def test_history_tool_exposed_when_supported(server):
    """The mock server enables history, so each server should expose its history tool."""
    impl, params = server
    async with connect(params) as session:
        names = await tool_names(session)
    assert HISTORY_TOOL[impl] in names


async def test_the_aggregate_argument_is_withheld_when_unsupported(server):
    """The mock advertises no aggregate functions, so neither server may offer
    `aggregate_function` on `read_opcua_history` (capability gating).

    Gating moved from the tool to the argument when the two history tools merged:
    the tool is offered because this server *does* support HistoricalAccess, and
    the one thing it cannot do is simply not on the menu. The positive cases live
    in ``e2e/test_aggregate_e2e.py``, against the aggregate-capable mock.
    """
    _impl, params = server
    async with connect(params) as session:
        listed = await session.list_tools()
    history = next(tool for tool in listed.tools if tool.name == "read_opcua_history")
    offered = set((history.input_schema or {}).get("properties", {}))
    assert "aggregate_function" not in offered, sorted(offered)
    assert "processing_interval" not in offered, sorted(offered)
    assert "num_values" in offered, "a raw history read must still be offered"


async def test_an_aggregate_call_against_a_server_without_them_errors_cleanly(server):
    """Asking for an aggregate anyway must say so, not crash or mislead.

    A client may hold a cached catalogue from a moment when the server did
    advertise aggregates, so the argument being withheld from tools/list is not
    the same as it being unreachable. Both runtimes recompute support on demand
    rather than trusting a cache that can go stale.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "read_opcua_history",
            {
                "node_id": NODE["Temperature"],
                "start_time": "2026-01-01T00:00:00Z",
                "aggregate_function": "Average",
                "processing_interval": 60000,
            },
        )
    assert result.is_error is True, impl
    assert "aggregate" in text_of(result).lower(), f"{impl}: {text_of(result)!r}"


async def test_read_single_node(server):
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["Temperature"]]})
    assert not result.is_error
    text = text_of(result)
    assert NODE["Temperature"] in text
    assert "value" in text.lower()


async def test_read_multiple_nodes(server):
    _impl, params = server
    ids = [NODE["Temperature"], NODE["Pressure"], NODE["PumpEnabled"]]
    async with connect(params) as session:
        result = await session.call_tool("read_opcua_nodes", {"node_ids": ids})
    assert not result.is_error
    text = text_of(result)
    for nid in ids:
        assert nid in text


async def test_browse_discovers_variables_below_a_root(server):
    """What `get_all_variables` was for, now a shape of `browse_opcua_nodes`."""
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "browse_opcua_nodes",
            {"depth": 4, "node_class": "Variable", "include_values": True},
        )
    assert not result.is_error, text_of(result)
    body = result.structured_content["result"]
    names = {node["browse_name"] for node in body["nodes"]}
    assert any("Temperature" in name for name in names), sorted(names)
    assert all(node["node_class"] == "Variable" for node in body["nodes"])
    # `include_values` means the values came back with them.
    assert any(node["value"] is not None for node in body["nodes"])


async def test_browse_honours_its_traversal_budget_and_says_so(server):
    """A walk that stopped early reports it, rather than looking complete.

    This is the same class of silent wrong answer as the missing
    continuation-point drain (#75): a prefix of the address space is
    indistinguishable from all of it unless the result says which it is.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "browse_opcua_nodes", {"max_nodes": 1, "depth": 3, "include_values": False}
        )
    assert not result.is_error, text_of(result)
    body = result.structured_content["result"]
    assert body["truncated"] is True, impl
    assert body["inspected"] <= 1, impl


async def test_browse_rejects_an_unknown_root(server):
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("browse_opcua_nodes", {"node_id": UNKNOWN_NODE})
    assert result.is_error is True, impl
    assert f"Failed to browse {UNKNOWN_NODE}" in text_of(result)


async def test_browse_finds_a_node_by_name(server):
    """`name_filter` is what issue #11 asked a second tool for."""
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "browse_opcua_nodes", {"depth": 4, "name_filter": "temperature"}
        )
    assert not result.is_error, text_of(result)
    nodes = result.structured_content["result"]["nodes"]
    assert nodes, "searching for 'temperature' found nothing"
    assert all("temperature" in node["browse_name"].lower() for node in nodes)


async def test_browse_children(server):
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "browse_opcua_nodes", {"node_id": NODE["IndustrialControlSystem"]}
        )
    assert not result.is_error
    text = text_of(result)
    for folder in ("Sensors", "Actuators", "SystemStatus", "Methods"):
        assert folder in text


async def test_write_numeric_node(server):
    """Writing a Double actuator should succeed (the sim may overwrite it later)."""
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ValvePosition"], "value": "80"}]},
        )
    assert not result.is_error, text_of(result)
    [record] = records_of(result)
    assert record["status"] == "Good", record


async def test_write_boolean_node(server):
    """Writing a Boolean node with 'true' must succeed (regression: bool handling)."""
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["StopProductionCommand"], "value": "true"}]},
        )
    assert not result.is_error, text_of(result)
    [record] = records_of(result)
    assert record["status"] == "Good", record


async def test_batch_write_uses_typed_values_in_one_operation(server):
    impl, params = server
    writes = [
        {"node_id": NODE["ScratchDouble"], "value": "42.25"},
        {"node_id": NODE["ScratchBoolean"], "value": "false"},
    ]
    async with connect(params) as session:
        written = await session.call_tool("write_opcua_nodes", {"nodes": writes})
        read = await session.call_tool(
            "read_opcua_nodes",
            {"node_ids": [NODE["ScratchDouble"], NODE["ScratchBoolean"]]},
        )
    assert not written.is_error, f"{impl}: {text_of(written)}"
    assert [record["status"] for record in records_of(written)] == ["Good", "Good"], (
        f"{impl}: {text_of(written)}"
    )
    # Read back, because the point is the *conversion*: both were sent as
    # strings and must arrive as a Double and a Boolean, not as "42.25" and
    # "false". A Good status says the server took the write, not what it stored.
    values = {record["node_id"]: record["value"] for record in records_of(read)}
    types = {record["node_id"]: record["data_type"] for record in records_of(read)}
    assert values[NODE["ScratchDouble"]] == 42.25, f"{impl}: {values}"
    assert types[NODE["ScratchDouble"]] == "Double", f"{impl}: {types}"
    assert values[NODE["ScratchBoolean"]] is False, f"{impl}: {values}"
    assert types[NODE["ScratchBoolean"]] == "Boolean", f"{impl}: {types}"


async def test_batch_write_keeps_valid_items_when_one_node_is_rejected(server):
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {
                "nodes": [
                    {"node_id": NODE["ScratchDouble"], "value": "31.5"},
                    {"node_id": UNKNOWN_NODE, "value": "1"},
                ]
            },
        )
        read = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchDouble"]]})
    assert not result.is_error, f"{impl}: {text_of(result)}"
    good, rejected = records_of(result)
    assert good["status"] == "Good", f"{impl}: {good}"
    assert rejected["node_id"] == UNKNOWN_NODE and rejected["status"].startswith("Bad"), (
        f"{impl}: {rejected}"
    )
    assert records_of(read)[0]["value"] == 31.5, (
        f"{impl}: valid batch member was not written: {text_of(read)}"
    )


async def test_call_method_start_then_stop(server):
    """Drive production via the StartProduction / StopProduction methods and check
    that SystemMode reacts. Exercises call_opcua_method + the method callbacks."""
    _impl, params = server
    async with connect(params) as session:
        methods = _discover_methods(await _browse_json(session, NODE["Methods"]))
        assert "StartProduction" in methods and "StopProduction" in methods

        start = await session.call_tool(
            "call_opcua_method",
            {
                "object_node_id": NODE["Methods"],
                "method_node_id": methods["StartProduction"],
                "arguments": ["60"],
            },
        )
        assert not start.is_error, text_of(start)

        mode = await wait_for_node_value(session, NODE["SystemMode"], "AUTO")
        assert "AUTO" in mode

        stop = await session.call_tool(
            "call_opcua_method",
            {"object_node_id": NODE["Methods"], "method_node_id": methods["StopProduction"]},
        )
        assert not stop.is_error, text_of(stop)

        mode2 = await wait_for_node_value(session, NODE["SystemMode"], "MANUAL")
        assert "MANUAL" in mode2


async def test_read_history(server):
    """Read recent history for the Temperature sensor and assert the canonical shape.

    Both servers must return the same records, field for field — see
    ``contract/tools.json`` -> ``resultShapes.historyRecords``. The schema-driven
    version of this check is in ``test_contract_parity.py``; what is asserted
    here is that real history reads through it correctly on both runtimes.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            HISTORY_TOOL[impl], {"node_id": NODE["Temperature"], "num_values": 5}
        )
    assert not result.is_error, text_of(result)

    records = records_of(result)
    assert records, f"{impl}: no history records returned"
    assert len(records) <= 5, f"{impl}: num_values=5 returned {len(records)} records"
    for record in records:
        assert set(record) == {"value", "timestamp", "status"}, (
            f"{impl}: record fields {sorted(record)} are not the contract's"
        )
        assert record["status"] == "Good", f"{impl}: unexpected status: {record}"
        # Temperature is a Double. `bool` is excluded because it is an `int` in
        # Python, and JSON has no integer/float distinction — a whole number
        # arrives as `int` from the Node server and `float` from the Python one.
        assert isinstance(record["value"], (int, float)) and not isinstance(
            record["value"], bool
        ), f"{impl}: value is not a number: {record!r}"
        assert ISO_UTC_TIMESTAMP.match(record["timestamp"]), (
            f"{impl}: timestamp is not ISO-8601 UTC: {record['timestamp']!r}"
        )

    # Not asserted: ordering. Given `num_values` alone, an OPC UA server reads
    # backwards from now, and both servers pass that ordering through unchanged.


async def test_history_rejects_a_malformed_timestamp_identically(server):
    """A bad timestamp must come back diagnosable, and worded the same on both.

    Two things could take this away silently. The `mcp` SDK only forwards a
    `ToolError`'s message to the client — any other exception is treated as a
    crash and replaced with `Error executing tool <name>`, which would leave the
    caller nothing to act on. And each server wraps the failure itself, so the
    `Failed to read node …` prefix is as much part of the shared wording as the
    `Invalid date/time: …` the unit tests pin. Asserting the whole sentence
    catches either one drifting.

    The SDKs' own outer prefixes are excluded: neither server chooses those.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            HISTORY_TOOL[impl],
            {"node_id": NODE["Temperature"], "start_time": "not-a-date"},
        )
    expected = (
        f"Failed to read history of node {NODE['Temperature']}: "
        'Invalid date/time: "not-a-date". Use ISO 8601, e.g. 2026-04-23T17:40:00Z'
    )
    assert expected in text_of(result), f"{impl}: got {text_of(result)!r}"
    assert result.is_error is True, f"{impl}: expected an error result"


async def test_write_rejects_a_bad_value_identically(server):
    """A value that will not convert is that node's failure, not the call's.

    It used to be the call's, on both runtimes, because there was only ever one
    node in flight. In a batch that would be wrong: one unconvertible value must
    not discard what the other writes did, the same reasoning that keeps a
    per-node server rejection inside a successful result. So it is now a record
    with a `BadTypeMismatch` status and an `error` saying why this server never
    sent it — a field that exists to separate "the server refused" from "we never
    asked".

    Only the reason text differs between runtimes: python-opcua says `could not
    convert string to float` and node-opcua `Cannot convert "…" to number`.
    Neither server chooses the other's wording, so only the status and the node
    are asserted.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ValvePosition"], "value": "not-a-number"}]},
        )
    assert not result.is_error, f"{impl}: one bad value must not fail the call"
    [record] = records_of(result)
    assert record["node_id"] == NODE["ValvePosition"], record
    assert record["status"] == "BadTypeMismatch", record
    assert record["error"], f"{impl}: a local failure must say why: {record}"


async def test_browse_rejects_an_unknown_node_identically(server):
    """A node the server does not have is an error, not an empty child list.

    This one was worse than a missing flag. python-opcua's `get_children()` never
    looks at `BrowseResult.StatusCode`, so the Python server answered
    `Children of ns=2;i=999999: []` — indistinguishable from a real node that has
    no children, and a successful result besides. `browse_children` in the Python
    server checks the status the library drops, so both now fail with the same
    sentence (bar the hex code node-opcua appends).
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("browse_opcua_nodes", {"node_id": UNKNOWN_NODE})
    text = text_of(result)
    assert f"Failed to browse {UNKNOWN_NODE}: Browse failed with status: Bad" in text, (
        f"{impl}: got {text!r}"
    )
    assert result.is_error is True, f"{impl}: expected an error result"


async def test_calling_a_method_on_an_unknown_node_fails_identically(server):
    """A method call that cannot resolve its nodes fails, on both (#63).

    Shared prefix only, and for the usual reason — each client library words the
    underlying rejection its own way — but both name the status.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "call_opcua_method",
            {
                "object_node_id": UNKNOWN_NODE,
                "method_node_id": UNKNOWN_NODE,
                "arguments": [],
            },
        )
    text = text_of(result)
    assert f"Failed to call method {UNKNOWN_NODE} on object {UNKNOWN_NODE}" in text, (
        f"{impl}: got {text!r}"
    )
    assert "BadNodeIdInvalid" in text, f"{impl}: got {text!r}"
    assert result.is_error is True, f"{impl}: expected an error result"


async def test_a_rejected_node_in_a_batch_read_stays_a_partial_result(server):
    """One bad node in `read_opcua_nodes` is a status, not an error.

    The counterpart to the three tests above, and the reason they stop where they
    do: the batch tools report per-node status *inside* a successful result, on
    both runtimes. Promoting a per-node rejection to an MCP error would discard
    the values of every other node in the batch. Only a failure of the whole
    operation is an error.

    The good node is asserted too — a batch that failed wholesale would also show
    no successful read, and this must not pass on those grounds.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "read_opcua_nodes", {"node_ids": [NODE["Temperature"], UNKNOWN_NODE]}
        )
    text = text_of(result)
    assert not result.is_error, f"{impl}: a per-node rejection must not fail the call: {text!r}"
    good, rejected = records_of(result)
    assert good["node_id"] == NODE["Temperature"] and good["status"] == "Good", f"{impl}: {good}"
    assert rejected["node_id"] == UNKNOWN_NODE, f"{impl}: {rejected}"
    assert rejected["status"].startswith("Bad"), f"{impl}: no per-node error status: {rejected}"
    assert rejected["value"] is None, f"{impl}: a rejected node must carry no value: {rejected}"


async def test_a_batch_read_that_fails_wholesale_is_an_error_on_both(server):
    """A batch read that cannot run at all is an MCP error, not a text result.

    The other half of the test above, and the one that was missing: a *per-node*
    rejection stays inside a successful result, but a failure of the whole
    operation must set `isError` so the model cannot reason over the excuse as
    if it were data.

    The Python server returned `"Error reading multiple nodes: …"` as a normal
    result until this test existed (#76). It was the last survivor of the sweep
    in #63, which fixed four sibling handlers and missed this one precisely
    because nothing asserted it.

    A node id that is not a node id at all fails before any request is sent, so
    both client libraries reject it locally and neither needs the mock's help.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("read_opcua_nodes", {"node_ids": ["not-a-node-id"]})
    text = text_of(result)
    assert result.is_error is True, f"{impl}: expected an error result, got {text!r}"
    assert "Failed to read nodes" in text, f"{impl}: got {text!r}"


# --- data-change subscriptions (issue #3) --------------------------------------


async def test_subscribe_observes_value_changes(server):
    """Subscribe to a live sensor and watch the values arrive without polling it.

    The mock's Temperature node moves once a second, so a subscription publishing
    every 200ms should accumulate several distinct readings.
    """
    impl, params = server
    async with connect(params) as session:
        created = await session.call_tool(
            "subscribe_opcua_nodes",
            {"node_ids": [NODE["Temperature"]], "publishing_interval": 200, "buffer_size": 10},
        )
        assert not created.is_error, text_of(created)

        [record] = records_of(created)
        assert record["node_id"] == NODE["Temperature"]
        assert record["publishing_interval"] == 200
        # 0 means "sample at the publishing interval", resolved before answering.
        assert record["sampling_interval"] == 200
        assert record["buffer_size"] == 10

        record = await wait_for_changes(session, record["subscription_id"], at_least=3)

    assert record["change_count"] >= 3, f"{impl}: only {record.get('change_count')} changes"
    changes = record["changes"]
    assert changes, f"{impl}: change_count moved but nothing was buffered"
    for change in changes:
        assert set(change) == {"value", "timestamp", "status"}, (
            f"{impl}: buffered change fields {sorted(change)} are not the contract's"
        )
        assert change["status"] == "Good", f"{impl}: unexpected status: {change}"
        assert isinstance(change["value"], (int, float)) and not isinstance(
            change["value"], bool
        ), f"{impl}: value is not a number: {change!r}"
        assert ISO_UTC_TIMESTAMP.match(change["timestamp"]), (
            f"{impl}: timestamp is not ISO-8601 UTC: {change['timestamp']!r}"
        )
    # The sensor really is moving, so the subscription is reporting changes
    # rather than the same reading over and over.
    assert len({change["value"] for change in changes}) > 1, (
        f"{impl}: every buffered value is identical: {changes!r}"
    )


async def test_buffer_size_bounds_what_is_retained(server):
    """`change_count` counts everything; `changes` keeps only the newest few."""
    impl, params = server
    async with connect(params) as session:
        created = await session.call_tool(
            "subscribe_opcua_nodes",
            {"node_ids": [NODE["Temperature"]], "publishing_interval": 200, "buffer_size": 2},
        )
        assert not created.is_error, text_of(created)
        [record] = records_of(created)
        record = await wait_for_changes(session, record["subscription_id"], at_least=4)

    assert record["change_count"] >= 4, f"{impl}: only {record.get('change_count')} changes"
    assert len(record["changes"]) == 2, (
        f"{impl}: buffer_size=2 retained {len(record['changes'])} changes"
    )


async def test_list_and_cancel_subscriptions(server):
    """Two subscriptions, both listed, one cancelled, the other left alone."""
    impl, params = server
    async with connect(params) as session:
        first = records_of(
            await session.call_tool("subscribe_opcua_nodes", {"node_ids": [NODE["Temperature"]]})
        )[0]
        second = records_of(
            await session.call_tool("subscribe_opcua_nodes", {"node_ids": [NODE["Pressure"]]})
        )[0]
        assert first["subscription_id"] != second["subscription_id"]

        listed = await session.call_tool("list_subscriptions", {})
        assert not listed.is_error, text_of(listed)
        by_id = {r["subscription_id"]: r for r in records_of(listed)}
        assert set(by_id) == {first["subscription_id"], second["subscription_id"]}
        assert by_id[first["subscription_id"]]["node_id"] == NODE["Temperature"]
        assert by_id[second["subscription_id"]]["node_id"] == NODE["Pressure"]

        cancelled = await session.call_tool(
            "unsubscribe_opcua_nodes", {"subscription_ids": [first["subscription_id"]]}
        )
        assert not cancelled.is_error, text_of(cancelled)
        assert first["subscription_id"] in text_of(cancelled)
        assert NODE["Temperature"] in text_of(cancelled)

        remaining = records_of(await session.call_tool("list_subscriptions", {}))
        assert [r["subscription_id"] for r in remaining] == [second["subscription_id"]], (
            f"{impl}: cancelling one subscription did not leave exactly the other"
        )


async def test_list_subscriptions_is_empty_before_subscribing(server):
    """No subscriptions means zero records, not an error and not a placeholder."""
    _impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("list_subscriptions", {})
    assert not result.is_error, text_of(result)
    assert records_of(result) == []


async def test_unsubscribe_rejects_an_unknown_id_identically(server):
    """Both servers refuse an unknown subscription with the same sentence.

    The same reasoning as the malformed-timestamp test above: the Python SDK only
    forwards a `ToolError`'s message, so anything else would reach the caller as
    `Error executing tool …` and leave them nothing to act on.
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "unsubscribe_opcua_nodes", {"subscription_ids": ["sub-9999"]}
        )
    assert "No such subscription: sub-9999" in text_of(result), f"{impl}: got {text_of(result)!r}"
    assert result.is_error is True, f"{impl}: expected an error result"


async def test_subscribing_to_an_unknown_node_fails_identically(server):
    """A node the server does not have must not leave a subscription behind.

    Only the prefix is shared: each OPC UA client library words the underlying
    rejection its own way, and both name the status (`BadNodeIdUnknown`).
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("subscribe_opcua_nodes", {"node_ids": ["ns=2;i=999999"]})
        text = text_of(result)
        assert "Failed to subscribe to node ns=2;i=999999" in text, f"{impl}: got {text!r}"
        assert "BadNodeIdUnknown" in text, f"{impl}: got {text!r}"
        assert result.is_error is True, f"{impl}: expected an error result"

        listed = await session.call_tool("list_subscriptions", {})
    assert records_of(listed) == [], f"{impl}: a failed subscribe left a subscription behind"


async def test_the_subscriptions_resource_tracks_the_tools(server):
    """The resource is the same state the tools report, re-readable for free."""
    impl, params = server
    async with connect(params) as session:
        listed = {str(r.uri) for r in (await session.list_resources()).resources}
        assert SUBSCRIPTIONS_URI in listed, f"{impl}: resources are {sorted(listed)}"

        before = json.loads((await session.read_resource(SUBSCRIPTIONS_URI)).contents[0].text)
        assert before == {"subscriptions": []}

        [record] = records_of(
            await session.call_tool(
                "subscribe_opcua_nodes",
                {"node_ids": [NODE["Temperature"]], "publishing_interval": 200},
            )
        )
        await wait_for_changes(session, record["subscription_id"], at_least=2)

        after = json.loads((await session.read_resource(SUBSCRIPTIONS_URI)).contents[0].text)
        assert [r["subscription_id"] for r in after["subscriptions"]] == [record["subscription_id"]]
        assert after["subscriptions"][0]["changes"], f"{impl}: the resource buffered nothing"

        await session.call_tool(
            "unsubscribe_opcua_nodes", {"subscription_ids": [record["subscription_id"]]}
        )
        emptied = json.loads((await session.read_resource(SUBSCRIPTIONS_URI)).contents[0].text)
    assert emptied == {"subscriptions": []}


async def test_subscriptions_do_not_survive_a_session(server):
    """Each MCP session starts clean, which is the visible half of teardown.

    A subscription left behind by the previous session would show up here — and
    the OPC UA server would still be publishing to it. The other half, that the
    subscription is actually deleted rather than merely forgotten, is asserted
    against a stand-in in the unit suites on both sides.
    """
    impl, params = server
    async with connect(params) as session:
        created = await session.call_tool(
            "subscribe_opcua_nodes", {"node_ids": [NODE["Temperature"]], "publishing_interval": 200}
        )
        assert not created.is_error, text_of(created)
        await asyncio.sleep(1)

    async with connect(params) as session:
        result = await session.call_tool("list_subscriptions", {})
    assert records_of(result) == [], f"{impl}: a subscription outlived its MCP session"


# --- browse parsing (server output formats differ) -----------------------------


async def _browse_json(session, node_id: str):
    """Return a list of {node_id, browse_name} dicts from a browse call.

    The Python server emits a Python ``repr`` of the list while the Node server
    emits JSON; this normalises both.
    """
    result = await session.call_tool("browse_opcua_nodes", {"node_id": node_id})
    text = text_of(result)
    blob = text[text.index("[") : text.rindex("]") + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        import ast

        return ast.literal_eval(blob)


def _discover_methods(children) -> dict[str, str]:
    """Map method browse-name -> node_id from browse output."""
    out = {}
    for child in children:
        name = str(child["browse_name"]).split(":")[-1]
        out[name] = child["node_id"]
    return out
