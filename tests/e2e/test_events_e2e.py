"""End-to-end tests for events and Alarms & Conditions (issue #4).

Split over two mock servers, because no single one of them can cover both halves:

* the **main** mock raises plain OPC UA events (it announces every change of its
  alarm state), which is what ``subscribe_events`` and ``read_events`` need —
  and it has no condition model at all, which is what makes it the right place
  to assert that ``list_active_alarms`` fails *legibly* against a server without
  Alarms & Conditions;
* the **alarms** mock (``packages/mock-server-alarms``, node-opcua) has a real
  ExclusiveLimitAlarm, so ``list_active_alarms`` and ``acknowledge_alarm`` are
  exercised against a genuine condition instance rather than against something
  shaped like our own idea of one.

Both are run against both MCP servers, and the records they return are checked
against the contract's ``resultShapes.eventRecords`` — the point of the shape
being declared once is that neither runtime can drift from it.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import ALARM_TEMPERATURE_NODE_ID
from test_contract_parity import assert_matches_result_shape
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, records_of, text_of

# The mock announces an alarm at severity 700 and its clearing at 100, so a
# subscription with this floor keeps the former and drops the latter.
ALARM_SEVERITY = 700

# The mock's simulation loop runs at 1 Hz and only then notices a command
# variable, sets the alarm state and raises the event. Two ticks of slack.
EVENT_SETTLE_SECONDS = 3

EVENT_TOOLS = {"subscribe_events", "read_events", "list_active_alarms", "acknowledge_alarm"}


@pytest.fixture(params=["python", "node"])
def server(request, opcua_server):
    """``(impl_name, StdioServerParameters)`` against the main mock."""
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, opcua_server)


@pytest.fixture(params=["python", "node"])
def alarm_server(request, alarm_opcua_server):
    """``(impl_name, StdioServerParameters)`` against the Alarms & Conditions mock."""
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, alarm_opcua_server)


async def _trigger_alarm(session) -> None:
    """Drive the main mock into its alarm state, via the emergency-stop command."""
    result = await session.call_tool(
        "write_opcua_node", {"node_id": NODE["EmergencyStopCommand"], "value": "true"}
    )
    assert not result.is_error, text_of(result)
    await asyncio.sleep(EVENT_SETTLE_SECONDS)


async def _reset_plant(session) -> None:
    """Put the mock back the way it was: no alarm, no emergency, MANUAL mode.

    The whole session shares one mock server, so a test that leaves the plant
    latched in MAINTENANCE hands the next one a different machine.
    """
    await session.call_tool(
        "write_opcua_node", {"node_id": NODE["ResetSystemCommand"], "value": "true"}
    )
    await asyncio.sleep(EVENT_SETTLE_SECONDS)


# --- plain events, against the main mock ---------------------------------------


async def test_event_tools_are_always_advertised(server):
    """Unlike history and aggregates, the event tools are not capability-gated.

    Every OPC UA server has a Server object with an EventNotifier, and a server
    that raises nothing simply buffers nothing — there is no capability to probe
    that would make hiding these tools more honest than offering them.
    """
    impl, params = server
    async with connect(params) as session:
        names = {t.name for t in (await session.list_tools()).tools}
    assert names >= EVENT_TOOLS, f"{impl}: missing event tools: {EVENT_TOOLS - names}"


async def test_subscribe_then_read_receives_an_event(server):
    """The acceptance criterion: subscribe, and receive what the server raises."""
    impl, params = server
    async with connect(params) as session:
        subscribed = await session.call_tool("subscribe_events", {})
        assert not subscribed.is_error, text_of(subscribed)
        assert "Subscribed to events" in text_of(subscribed)

        try:
            await _trigger_alarm(session)
            result = await session.call_tool("read_events", {})
        finally:
            await _reset_plant(session)

    assert not result.is_error, text_of(result)
    records = records_of(result)
    assert records, f"{impl}: no events received after the alarm was raised"

    assert_matches_result_shape(records, "eventRecords", f"{impl}/read_events")

    alarms = [r for r in records if "Alarm active" in (r["message"] or "")]
    assert alarms, f"{impl}: no alarm event among {[r['message'] for r in records]}"
    raised = alarms[0]
    assert raised["severity"] == ALARM_SEVERITY
    assert raised["source_name"] == "IndustrialControlSystem"
    assert raised["event_type"] == "ns=0;i=2041", "a plain event is a BaseEventType"
    # A plain event carries no condition, and says so in every condition field
    # rather than leaving them out — that is what makes one record shape workable.
    for field in ("condition_id", "condition_name", "active", "acked", "retain"):
        assert raised[field] is None, f"{impl}: {field} should be null on a plain event"


async def test_reading_twice_drains_the_buffer(server):
    """Events are handed over once: a second read returns only what is new."""
    _impl, params = server
    async with connect(params) as session:
        await session.call_tool("subscribe_events", {})
        try:
            await _trigger_alarm(session)
            first = records_of(await session.call_tool("read_events", {}))
            second = records_of(await session.call_tool("read_events", {}))
        finally:
            await _reset_plant(session)

    assert first, "no events to drain"
    first_ids = {r["event_id"] for r in first}
    assert not first_ids & {r["event_id"] for r in second}, "an event was handed over twice"


async def test_severity_floor_drops_quieter_events(server):
    """`severity_min` keeps the alarm (700) and drops its clearing (100)."""
    _impl, params = server
    async with connect(params) as session:
        await session.call_tool("subscribe_events", {"severity_min": ALARM_SEVERITY})
        await _trigger_alarm(session)
        await _reset_plant(session)  # raises the quieter "Alarm cleared" event
        records = records_of(await session.call_tool("read_events", {}))

    assert records, "the alarm event itself should have been kept"
    assert all(r["severity"] >= ALARM_SEVERITY for r in records), (
        f"an event below the floor was buffered: {[r['severity'] for r in records]}"
    )
    assert all("cleared" not in (r["message"] or "") for r in records)


async def test_reading_without_subscribing_says_so(server):
    """Both runtimes word the mistake the same way, because a model will make it."""
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("read_events", {"node_id": NODE["Methods"]})
    expected = f"Not subscribed to events from node {NODE['Methods']}. Call subscribe_events first."
    assert expected in text_of(result), f"{impl}: got {text_of(result)!r}"


async def test_an_overflowing_buffer_tells_the_caller_what_it_lost(server):
    """A lost alarm must not read as quiet — the notice comes back to the caller.

    Only stderr carried this at first, which an MCP client never shows: an agent
    could not tell a complete event stream from one that dropped events during a
    burst. Both runtimes now append the same sentence to the response.
    """
    impl, params = server
    async with connect(params) as session:
        # A buffer of one, then two events: the first is dropped by the second.
        await session.call_tool("subscribe_events", {"buffer_size": 1})
        try:
            await _trigger_alarm(session)
            await _reset_plant(session)
            result = await session.call_tool("read_events", {})
        finally:
            await _reset_plant(session)

    assert not result.is_error, text_of(result)
    blocks = [b.text for b in result.content]
    assert len(blocks) == 2, f"{impl}: expected one event and one notice, got {blocks}"
    assert json.loads(blocks[0])["severity"] is not None, "the surviving event comes first"
    assert blocks[1] == (
        "Note: 1 older event(s) were dropped before this read — the buffer of 1 "
        "filled up. Raise buffer_size or read more often."
    ), f"{impl}: got {blocks[1]!r}"


async def test_a_refresh_that_never_finishes_is_an_error(alarm_server):
    """A partial ConditionRefresh must not be handed over as if it were complete.

    Timing out used to resolve exactly as a RefreshEnd did, so a slow server — or
    a short `timeout_seconds` — turned "I could not finish asking" into "no
    alarms", which is the one answer this tool must never invent. A zero timeout
    against a server that *does* have a retained alarm pins the distinction:
    the refresh is accepted, nothing arrives in time, and the caller is told so.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        result = await session.call_tool("list_active_alarms", {"timeout_seconds": 0})

    message = text_of(result)
    assert "ConditionRefresh did not finish within 0" in message, f"{impl}: got {message!r}"
    assert "Retry with a larger timeout_seconds" in message, f"{impl}: got {message!r}"


async def test_a_server_without_conditions_says_so(server):
    """python-opcua implements no ConditionRefresh, and the message must show it.

    A server with no Alarms & Conditions is the common case in the field, so what
    matters is that the model is told *why* it got nothing rather than being
    handed an empty list it would read as "no alarms".
    """
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool("list_active_alarms", {"timeout_seconds": 2})
    message = text_of(result)
    assert "ConditionRefresh failed" in message, f"{impl}: got {message!r}"
    assert "may not implement OPC UA Alarms & Conditions" in message, f"{impl}: got {message!r}"


# --- alarms and conditions, against the alarms mock ------------------------------


async def test_lists_and_acknowledges_a_real_alarm(alarm_server):
    """The other two acceptance criteria, end to end against a real condition.

    Re-arms the alarm first — write below the limit, then above it — so the test
    owns a freshly unacknowledged alarm no matter what ran before it, including
    the other runtime's turn through this same test.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        await session.call_tool(
            "write_opcua_node", {"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "20"}
        )
        await asyncio.sleep(1)
        await session.call_tool(
            "write_opcua_node", {"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "100"}
        )
        await asyncio.sleep(1)

        listed = await session.call_tool("list_active_alarms", {})
        assert not listed.is_error, text_of(listed)
        alarms = records_of(listed)
        assert alarms, f"{impl}: the mock's alarm should be active and retained"
        assert_matches_result_shape(alarms, "eventRecords", f"{impl}/list_active_alarms")

        alarm = next(a for a in alarms if a["condition_name"] == "HighTemperatureAlarm")
        assert alarm["condition_id"], "a condition must be identified for acknowledgement"
        assert alarm["active"] is True
        assert alarm["acked"] is False, f"{impl}: the re-armed alarm should be unacknowledged"
        assert alarm["retain"] is True

        # Only the event_id: the condition behind it is the server's to remember.
        acknowledged = await session.call_tool(
            "acknowledge_alarm",
            {"event_id": alarm["event_id"], "comment": f"acknowledged by the {impl} e2e test"},
        )
        assert not acknowledged.is_error, text_of(acknowledged)
        assert alarm["condition_id"] in text_of(acknowledged)

        after = records_of(await session.call_tool("list_active_alarms", {}))
        acked = next(a for a in after if a["condition_id"] == alarm["condition_id"])
        assert acked["acked"] is True, f"{impl}: the alarm is still unacknowledged"


async def test_an_unknown_event_id_cannot_be_acknowledged(alarm_server):
    """An event_id this server never handed out has no condition to act on."""
    impl, params = alarm_server
    async with connect(params) as session:
        result = await session.call_tool(
            "acknowledge_alarm", {"event_id": "bm90LWFuLWV2ZW50", "comment": "…"}
        )
    assert 'Unknown event_id "bm90LWFuLWV2ZW50"' in text_of(result), f"{impl}: {text_of(result)!r}"


async def test_condition_events_reach_the_buffer_too(alarm_server):
    """A condition is an event: `read_events` sees it, with its condition fields."""
    impl, params = alarm_server
    async with connect(params) as session:
        await session.call_tool(
            "write_opcua_node", {"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "20"}
        )
        await asyncio.sleep(1)
        await session.call_tool("subscribe_events", {})
        await session.call_tool(
            "write_opcua_node", {"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "100"}
        )
        await asyncio.sleep(1.5)
        records = records_of(await session.call_tool("read_events", {}))

    assert records, f"{impl}: no events after the alarm went active"
    assert_matches_result_shape(records, "eventRecords", f"{impl}/read_events")
    conditions = [r for r in records if r["condition_id"] is not None]
    assert conditions, f"{impl}: no condition event among {[r['event_type'] for r in records]}"
    assert conditions[-1]["active"] is True
    assert conditions[-1]["condition_name"] == "HighTemperatureAlarm"
