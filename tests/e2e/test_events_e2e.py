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
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["EmergencyStopCommand"], "value": "true"}]},
    )
    assert not result.is_error, text_of(result)
    await asyncio.sleep(EVENT_SETTLE_SECONDS)


async def _reset_plant(session) -> None:
    """Put the mock back the way it was: no alarm, no emergency, MANUAL mode.

    The whole session shares one mock server, so a test that leaves the plant
    latched in MAINTENANCE hands the next one a different machine.
    """
    await session.call_tool(
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ResetSystemCommand"], "value": "true"}]},
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
        # A record rather than a sentence: it reports the values actually in
        # force after clamping, which is what a caller has to know.
        setup = subscribed.structured_content["result"]
        assert setup["node_id"] == "ns=0;i=2253", setup
        assert setup["buffer_size"] > 0, setup
        assert setup["replaced"] is False, setup

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
            "write_opcua_nodes",
            {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "20"}]},
        )
        await asyncio.sleep(1)
        await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "100"}]},
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
    assert result.is_error is True, f"{impl}: expected an error result"


async def test_condition_events_reach_the_buffer_too(alarm_server):
    """A condition is an event: `read_events` sees it, with its condition fields."""
    impl, params = alarm_server
    async with connect(params) as session:
        await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "20"}]},
        )
        await asyncio.sleep(1)
        await session.call_tool("subscribe_events", {})
        await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "100"}]},
        )
        await asyncio.sleep(1.5)
        records = records_of(await session.call_tool("read_events", {}))

    assert records, f"{impl}: no events after the alarm went active"
    assert_matches_result_shape(records, "eventRecords", f"{impl}/read_events")
    conditions = [r for r in records if r["condition_id"] is not None]
    assert conditions, f"{impl}: no condition event among {[r['event_type'] for r in records]}"
    assert conditions[-1]["active"] is True
    assert conditions[-1]["condition_name"] == "HighTemperatureAlarm"


# --- the rest of the operator workflow (#119) -------------------------------------


async def _rearm(session) -> dict:
    """An alarm that is active, unacknowledged and this test's own.

    Write below the limit, then above it, so the test owns a freshly raised alarm
    whatever ran before it — including the other runtime's turn through the same
    test.
    """
    await session.call_tool(
        "write_opcua_nodes",
        {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "20"}]},
    )
    await asyncio.sleep(1)
    await session.call_tool(
        "write_opcua_nodes",
        {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": "100"}]},
    )
    await asyncio.sleep(1)
    alarms = records_of(await session.call_tool("list_active_alarms", {}))
    return next(a for a in alarms if a["condition_name"] == "HighTemperatureAlarm")


async def test_the_acknowledge_confirm_handshake_has_both_stages(alarm_server):
    """Part 9 §5.5 defines acknowledge→confirm, and only the first half existed.

    An agent could say "I have seen this" and then had no way to say "I have
    dealt with it" — which is the stage that actually clears an operator's queue.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        alarm = await _rearm(session)

        acknowledged = await session.call_tool(
            "acknowledge_alarm",
            {"event_id": alarm["event_id"], "comment": f"seen by the {impl} e2e test"},
        )
        assert not acknowledged.is_error, text_of(acknowledged)

        # Re-list for the event_id the *acknowledgement* produced. Every condition
        # state change is its own event with its own EventId, and Part 9 methods
        # only accept a current one — confirming with the pre-acknowledge id is
        # answered `BadEventIdUnknown`. This is the one piece of the workflow that
        # is not obvious from the tool's arguments, which is why the description
        # says it too.
        await asyncio.sleep(1)
        after_ack = records_of(await session.call_tool("list_active_alarms", {}))
        acked = next(a for a in after_ack if a["condition_id"] == alarm["condition_id"])
        assert acked["acked"] is True, f"{impl}: the alarm was not acknowledged"

        confirmed = await session.call_tool(
            "act_on_alarm",
            {
                "event_id": acked["event_id"],
                "action": "confirm",
                "comment": f"dealt with by the {impl} e2e test",
            },
        )
        assert not confirmed.is_error, f"{impl}: {text_of(confirmed)}"

    record = json.loads(text_of(confirmed))
    assert record["action"] == "confirm", f"{impl}: {record}"
    assert record["status"] == "Good", f"{impl}: {record}"
    assert record["condition_id"] == alarm["condition_id"], f"{impl}: {record}"


async def test_a_comment_can_be_left_without_changing_the_alarms_state(alarm_server):
    """AddComment is a method of ConditionType itself, so every condition has it."""
    impl, params = alarm_server
    async with connect(params) as session:
        alarm = await _rearm(session)

        result = await session.call_tool(
            "act_on_alarm",
            {
                "event_id": alarm["event_id"],
                "action": "comment",
                "comment": "investigating: valve V-12 may be stuck",
            },
        )
        assert not result.is_error, f"{impl}: {text_of(result)}"

        after = records_of(await session.call_tool("list_active_alarms", {}))
        still = next(a for a in after if a["condition_id"] == alarm["condition_id"])

    # A note, not a state change: the alarm is as unacknowledged as it was.
    assert still["acked"] is False, f"{impl}: commenting acknowledged the alarm"
    assert still["active"] is True, f"{impl}: commenting cleared the alarm"


# node-opcua implements the ShelvingState machine, with one gap. Measured on both
# runtimes, with the routing correct:
#
#   TimedShelve    => Good, and the condition really is shelved afterwards
#   Unshelve       => Good when shelved, BadConditionNotShelved when not
#   OneShotShelve  => BadInternalError, always
#
# So `shelveFor` and `unshelve` are asserted functionally below, and
# `OneShotShelve` gets a test that asserts what is left: that the request is
# *routed* correctly. That matters because getting it wrong does not fail cleanly.
# The shelving methods belong to ShelvedStateMachineType and hang off the
# condition's ShelvingState component, not off the condition; resolved against the
# wrong object, the server finds a *different* method of the right name's
# neighbour and answers BadArgumentsMissing or BadTooManyArguments. Those were the
# answers this gave while the Node runtime's routing was still wrong — which is
# also why the shelving asymmetry between the two runtimes was worth chasing
# rather than writing off as the mock's mood.
WRONG_OBJECT = ("BadArgumentsMissing", "BadTooManyArguments")


async def test_a_timed_shelve_really_shelves_and_unshelving_brings_it_back(alarm_server):
    """Shelving is what an operator does with a chattering nuisance alarm.

    'This level switch has cycled 40 times in an hour, shelve it for 30 minutes'
    is a natural agent action, and it was the one piece of the workflow with no
    way to express it at all. `shelveFor` is also self-limiting — the alarm comes
    back on its own whether or not anyone remembers to unshelve it — which is a
    rare and welcome property in something handed to an agent.

    The shelve is proved rather than assumed: unshelving an *unshelved* condition
    is an error, so the successful unshelve in the middle can only have happened
    because the shelve before it moved the state machine.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        alarm = await _rearm(session)
        # The mock is session-scoped and shared with the other runtime's turn, so
        # start from a known state rather than assuming one.
        await session.call_tool(
            "act_on_alarm", {"event_id": alarm["event_id"], "action": "unshelve"}
        )

        shelved = await session.call_tool(
            "act_on_alarm",
            {
                "event_id": alarm["event_id"],
                "action": "shelveFor",
                "shelve_duration_ms": 30_000,
            },
        )
        assert not shelved.is_error, f"{impl}: {text_of(shelved)}"
        record = json.loads(text_of(shelved))
        assert record["action"] == "shelveFor", f"{impl}: {record}"
        assert record["status"] == "Good", f"{impl}: {record}"
        assert record["condition_id"] == alarm["condition_id"], f"{impl}: {record}"

        back = await session.call_tool(
            "act_on_alarm", {"event_id": alarm["event_id"], "action": "unshelve"}
        )
        assert not back.is_error, f"{impl}: {text_of(back)}"
        assert json.loads(text_of(back))["action"] == "unshelve"

        again = await session.call_tool(
            "act_on_alarm", {"event_id": alarm["event_id"], "action": "unshelve"}
        )

    assert again.is_error, f"{impl}: unshelving twice should be refused"
    assert "NotShelved" in text_of(again), f"{impl}: {text_of(again)}"


async def test_a_one_shot_shelve_reaches_the_shelving_state_machine(alarm_server):
    """The one shelving action node-opcua does not implement. See the note above.

    Routed correctly, which is the part this project owns: if node-opcua ever
    implements it, this fails and should be upgraded to assert the shelved state
    the way the timed test does.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        alarm = await _rearm(session)
        await session.call_tool(
            "act_on_alarm", {"event_id": alarm["event_id"], "action": "unshelve"}
        )

        result = await session.call_tool(
            "act_on_alarm", {"event_id": alarm["event_id"], "action": "shelve"}
        )

    text = text_of(result)
    for wrong in WRONG_OBJECT:
        assert wrong not in text, (
            f"{impl}: {wrong} means OneShotShelve was resolved against the condition "
            f"instead of its ShelvingState — see the note above. Got: {text!r}"
        )
    assert "BadInternalError" in text, (
        f"{impl}: expected node-opcua's unimplemented-OneShotShelve answer, got {text!r}. "
        f"Good means it now implements it, and this should assert the shelved state instead."
    )
    assert alarm["condition_id"] in text, f"{impl}: {text}"


async def test_shelve_and_shelve_for_are_one_argument_apart_and_say_so(alarm_server):
    """A duration on 'shelve' would be silently ignored, and its absence on
    'shelveFor' would silently shelve forever. Both are refused, naming the
    action the caller probably meant."""
    impl, params = alarm_server
    async with connect(params) as session:
        alarm = await _rearm(session)

        no_duration = await session.call_tool(
            "act_on_alarm", {"event_id": alarm["event_id"], "action": "shelveFor"}
        )
        stray_duration = await session.call_tool(
            "act_on_alarm",
            {"event_id": alarm["event_id"], "action": "shelve", "shelve_duration_ms": 1000},
        )

    assert no_duration.is_error, impl
    assert text_of(no_duration) == (
        'act_on_alarm requires shelve_duration_ms with action "shelveFor". Use action '
        '"shelve" to shelve until the alarm returns to normal instead.'
    ), f"{impl}: {text_of(no_duration)}"

    assert stray_duration.is_error, impl
    assert text_of(stray_duration) == (
        'act_on_alarm only takes shelve_duration_ms with action "shelveFor"; got "shelve". '
        'Use "shelveFor" to shelve for a set time, or drop the duration to shelve until '
        "the alarm clears."
    ), f"{impl}: {text_of(stray_duration)}"


async def test_an_action_opc_ua_does_not_have_is_refused_by_the_schema(alarm_server):
    """The contract's `enum`, doing the job it was added for in #114."""
    impl, params = alarm_server
    async with connect(params) as session:
        result = await session.call_tool(
            "act_on_alarm", {"event_id": "bm90LWFuLWV2ZW50", "action": "suppress"}
        )

    assert result.is_error, impl
    assert "must be one of" in text_of(result), f"{impl}: {text_of(result)}"
    assert '"suppress"' in text_of(result), f"{impl}: {text_of(result)}"
