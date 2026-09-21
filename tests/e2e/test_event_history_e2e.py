"""Looking backwards at an alarm burst (issue #117).

``subscribe_events`` only sees what arrives after it subscribes. That is the
right shape for watching a plant and the wrong shape for the question people
actually bring to an assistant, which is always about the past: what fired
overnight, what happened in the ten minutes before the line stopped. By the time
anyone asks, those events are gone.

OPC UA Part 11 §6.5.2 answers it with ``ReadEventDetails``, and a server that
historises its events already holds what is being asked for.

What is worth asserting end to end, given the unit half covers the severity rule:

* that the request reaches a real server and comes back with real events, rather
  than an empty list that a broken filter would produce just as quietly;
* that a historical event is the **same record** as a live one — this is the
  whole design constraint, and it is why both paths share the select clauses and
  the decoder rather than each building their own;
* that the capability gate matches what the server actually does, in both
  directions: the main mock historises events and the alarms mock does not.

The main mock had to be taught to keep its events for this (see
``_historize_events`` there) — python-opcua stores nothing unless the source
declares ``GeneratesEvent``, and it has no ``AccessHistoryEventsCapability`` node
in namespace 0 at all.

Run:
    cd tests && uv run --no-sync pytest e2e/test_event_history_e2e.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from test_contract_parity import assert_matches_result_shape
from test_events_e2e import EVENT_SETTLE_SECONDS, _reset_plant, _trigger_alarm
from test_mcp_e2e import NODE_BUILD, _server_params, connect, text_of


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


async def history(session, arguments: dict | None = None) -> list[dict]:
    """One `read_event_history` call, asserting it succeeded."""
    result = await session.call_tool("read_event_history", arguments or {})
    assert not result.is_error, text_of(result)
    return result.structured_content["result"]


async def test_it_finds_events_that_were_raised_before_anyone_subscribed(server):
    """The acceptance criterion, and the thing subscribe_events cannot do.

    No subscription is opened at any point here. The alarm is triggered, and then
    it is read back out of the server's archive — which is exactly the situation
    an operator is in when they arrive after the fact.
    """
    impl, params = server
    async with connect(params) as session:
        try:
            await _trigger_alarm(session)
            records = await history(session)
        finally:
            await _reset_plant(session)

    messages = [record["message"] for record in records]
    assert any("Alarm active" in (m or "") for m in messages), f"{impl}: {messages}"


async def test_a_historical_event_is_the_same_record_as_a_live_one(server):
    """The design constraint: one event shape, whether watched or recovered.

    If the two differed, an agent would have to learn which tool produced a
    record before it could read it — and the fields it cares about (`severity`,
    `source_name`, `event_id`) are exactly the ones a separately-built filter
    would get subtly wrong. Both paths therefore send the same select clauses
    and run the same decoder; this is what says so from outside.
    """
    impl, params = server
    async with connect(params) as session:
        try:
            subscribed = await session.call_tool("subscribe_events", {})
            assert not subscribed.is_error, text_of(subscribed)
            await _trigger_alarm(session)

            live = await session.call_tool("read_events", {})
            assert not live.is_error, text_of(live)
            live_records = live.structured_content["result"]

            stored = await history(session)
        finally:
            await _reset_plant(session)

    assert live_records, f"{impl}: the live subscription saw nothing to compare against"
    assert stored, f"{impl}: nothing in the archive to compare against"
    assert set(live_records[0]) == set(stored[0]), impl

    def alarm_of(records):
        return next(r for r in records if "Alarm active" in (r["message"] or ""))

    seen, recovered = alarm_of(live_records), alarm_of(stored)
    for field in ("message", "severity", "source_name", "source_node", "event_type"):
        assert seen[field] == recovered[field], f"{impl}: {field} differs"


async def test_the_records_match_the_contract_shape(server):
    """The same `resultShapes.eventRecords` both runtimes are held to elsewhere."""
    _impl, params = server
    async with connect(params) as session:
        try:
            await _trigger_alarm(session)
            records = await history(session)
        finally:
            await _reset_plant(session)

    assert records
    assert_matches_result_shape(records, "eventRecords", "read_event_history")


async def test_severity_min_drops_the_quieter_events(server):
    """The mock announces an alarm at 700 and its clearing at 100."""
    impl, params = server
    async with connect(params) as session:
        try:
            await _trigger_alarm(session)
            await _reset_plant(session)
            await asyncio.sleep(EVENT_SETTLE_SECONDS)

            everything = await history(session)
            urgent = await history(session, {"severity_min": 500})
        finally:
            await _reset_plant(session)

    severities = [r["severity"] for r in everything]
    assert any(s is not None and s < 500 for s in severities), f"{impl}: {severities}"
    assert urgent, f"{impl}: the severity floor dropped everything"
    assert all(r["severity"] >= 500 for r in urgent), [r["severity"] for r in urgent]


async def test_a_range_in_the_past_finds_nothing_rather_than_everything(server):
    """An empty answer is an answer, and must not be the whole archive.

    A time range the implementation ignored would return today's events for a
    window in 2020 — which reads as a successful answer to a different question,
    and is the failure mode a test that only ever asks for "recent" would miss.
    """
    impl, params = server
    async with connect(params) as session:
        try:
            await _trigger_alarm(session)
            records = await history(
                session,
                {"start_time": "2020-01-01T00:00:00Z", "end_time": "2020-01-02T00:00:00Z"},
            )
        finally:
            await _reset_plant(session)

    assert records == [], f"{impl}: a 2020 window returned {len(records)} events"


async def test_it_is_offered_where_the_server_keeps_events(server):
    impl, params = server
    async with connect(params) as session:
        names = {t.name for t in (await session.list_tools()).tools}
    assert "read_event_history" in names, impl


async def test_it_is_hidden_where_the_server_does_not(alarm_server):
    """The gate has to be false somewhere, or it is not a gate.

    The alarms mock is a real node-opcua server with a real condition model and
    no event archive, so it is the honest negative case — better than a stub,
    because a stub would only prove the gate reads the flag this project writes.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert "read_event_history" not in names, impl

        # Catalog filtering is not enforcement: a client may hold a stale
        # tools/list. The call itself has to be refused too, which is the lesson
        # of #83 applied to the new gate.
        result = await session.call_tool("read_event_history", {})
    assert result.is_error is True, impl


async def test_both_runtimes_return_the_same_events(opcua_server):
    """One archive, two clients, and no room for them to describe it differently."""
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    async with connect(_server_params("python", opcua_server)) as session:
        await _trigger_alarm(session)
        await _reset_plant(session)
        await asyncio.sleep(EVENT_SETTLE_SECONDS)

    answers = {}
    for impl in ("python", "node"):
        async with connect(_server_params(impl, opcua_server)) as session:
            answers[impl] = [
                (r["message"], r["severity"], r["source_name"]) for r in await history(session)
            ]

    assert answers["python"], "nothing in the archive to compare"
    assert answers["python"] == answers["node"]
