"""End-to-end tests for connection resilience (issue #18).

The claim under test is the one an operator actually cares about: restart the OPC
UA server and the MCP server keeps working, without being restarted itself. So
each test here takes a mock away, gives it back on the same endpoint, and asks
the MCP server to do its job again.

That needs a mock of its own — `restartable_opcua_server`, function-scoped —
because stopping the session-wide one would break every other test using it.

Run:
    cd tests && uv run --no-sync pytest e2e/test_resilience_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from test_contract_parity import CONTRACT, normalized_catalogue
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, records_of, text_of

# Retry settings for these tests: the defaults are tuned for a plant (seconds of
# backoff, so a blip costs nothing), which would only make the suite slow. The
# point of overriding them here is itself an assertion — that the settings the
# issue asks for are configurable at all, and that the servers honour them.
FAST_RETRY = {
    "OPCUA_RECONNECT_INITIAL_DELAY_MS": "200",
    "OPCUA_RECONNECT_MAX_DELAY_MS": "1000",
    "OPCUA_RECONNECT_MAX_RETRY": "8",
}


def params_for(impl: str, url: str, **env):
    params = _server_params(impl, url)
    params.env.update(FAST_RETRY)
    params.env.update(env)
    return params


@pytest.fixture(params=["python", "node"])
def impl(request):
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return request.param


async def read_until_ok(session, node_id: str, attempts: int = 10, delay: float = 1.0):
    """Read `node_id`, retrying while the server is still coming back.

    Recovery is not instantaneous and is not meant to be: a tool call may land
    while the connection is still being rebuilt, and the honest answer then is an
    error. What resilience promises is that a *later* call succeeds without the
    MCP server having been restarted — so the assertion is about eventually, and
    this is how long "eventually" is allowed to be.
    """
    result = None
    for _ in range(attempts):
        result = await session.call_tool("read_opcua_nodes", {"node_ids": [node_id]})
        if not result.is_error:
            return result
        await asyncio.sleep(delay)
    return result


async def test_reads_recover_after_the_server_restarts(impl, restartable_opcua_server):
    server = restartable_opcua_server
    async with connect(params_for(impl, server.url)) as session:
        before = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["Temperature"]]})
        assert not before.is_error, text_of(before)

        server.restart()

        after = await read_until_ok(session, NODE["Temperature"])
        assert not after.is_error, f"{impl}: never recovered: {text_of(after)}"
        assert records_of(after)[0]["status"] == "Good", text_of(after)


async def test_writes_recover_after_the_server_restarts(impl, restartable_opcua_server):
    """Not just reads: the write path re-establishes a dead session too.

    Note what recovery means here, because it changed in #106. The server does
    *not* re-send a write whose outcome it does not know: `write_opcua_nodes` is
    `retryPolicy: uncertainOutcome`, so a call that dies mid-request rebuilds the
    connection and then says the outcome is unknown rather than actuating the
    plant a second time on a guess. What is promised is that a *later* call
    succeeds without the MCP server having been restarted — which is what the
    loop below asks for, and is the decision a caller is entitled to take and the
    server is not.
    """
    server = restartable_opcua_server
    async with connect(params_for(impl, server.url)) as session:
        server.restart()

        result = None
        for _ in range(10):
            result = await session.call_tool(
                "write_opcua_nodes",
                {"nodes": [{"node_id": NODE["ScratchDouble"], "value": 42.5}]},
            )
            if not result.is_error:
                break
            await asyncio.sleep(1.0)

        assert not result.is_error, f"{impl}: write never recovered: {text_of(result)}"
        # ScratchDouble rather than an actuator: the mock republishes every
        # actuator from its own state once a second, so reading one back after a
        # write races that timer. Nothing touches this node but the test.
        readback = await read_until_ok(session, NODE["ScratchDouble"])
        assert records_of(readback)[0]["value"] == 42.5, text_of(readback)


async def test_status_reports_the_reconnection(impl, restartable_opcua_server):
    """`get_server_status` is the tool an operator reaches for during an outage.

    It must report the truth on both sides of one: connected before, connected
    again afterwards — and to a server whose own start time has moved, which is
    what proves the session was re-established rather than merely believed in.
    """
    from test_diagnostics_e2e import status_of

    server = restartable_opcua_server
    async with connect(params_for(impl, server.url)) as session:
        before = status_of(await session.call_tool("get_server_status", {}))
        assert before["connected"] is True

        server.restart()

        after = None
        for _ in range(10):
            after = status_of(await session.call_tool("get_server_status", {}))
            if after["connected"]:
                break
            await asyncio.sleep(1.0)

        assert after["connected"] is True, f"{impl}: never reconnected: {after['error']}"
        assert after["start_time"] > before["start_time"], (
            f"{impl}: the server's start time did not move ({before['start_time']} -> "
            f"{after['start_time']}), so this is the old session, not a new one"
        )


async def test_subscriptions_are_re_established_after_a_restart(impl, restartable_opcua_server):
    """A subscription handle must keep working across an outage.

    An OPC UA subscription belongs to the session that created it, so a restart
    destroys it. Without re-establishment the agent would be left holding an ID
    that `list_subscriptions` still reports and that never delivers another value
    again — the worst of both worlds. The buffered changes from before the outage
    survive; only the gap is missing.
    """
    server = restartable_opcua_server
    async with connect(params_for(impl, server.url)) as session:
        created = await session.call_tool(
            "subscribe_opcua_nodes",
            {"node_ids": [NODE["Temperature"]], "publishing_interval": 200, "buffer_size": 50},
        )
        assert not created.is_error, text_of(created)
        subscription_id = records_of(created)[0]["subscription_id"]
        await asyncio.sleep(2)

        listed = records_of(await session.call_tool("list_subscriptions", {}))
        before = next(r for r in listed if r["subscription_id"] == subscription_id)
        assert before["change_count"] >= 1, before

        server.restart()
        # Bring the connection back: the servers reconnect on a tool call, not on
        # a timer, and `get_server_status` is the cheapest call that does it.
        for _ in range(10):
            status = await session.call_tool("get_server_status", {})
            if '"connected": true' in text_of(status):
                break
            await asyncio.sleep(1.0)

        after = {}
        for _ in range(15):
            await asyncio.sleep(1.0)
            listed = records_of(await session.call_tool("list_subscriptions", {}))
            after = next(
                (r for r in listed if r["subscription_id"] == subscription_id),
                {},
            )
            if after.get("change_count", 0) > before["change_count"]:
                break

        assert after, f"{impl}: subscription {subscription_id} disappeared across the restart"
        assert after["node_id"] == before["node_id"]
        assert after["publishing_interval"] == before["publishing_interval"]
        assert after["change_count"] > before["change_count"], (
            f"{impl}: subscription {subscription_id} stopped delivering after the restart "
            f"({before['change_count']} -> {after.get('change_count')})"
        )


async def test_event_subscriptions_are_re_established_after_a_restart(
    impl, restartable_opcua_server
):
    """The promise data-change subscriptions make, kept for events too (#157).

    The runtimes used to break it in two different ways. Node dropped the buffer
    and answered "not subscribed"; Python kept draining a buffer bound to the dead
    session and answered `[]` — "nothing happened" — for as long as anyone asked,
    which for an alarm stream is the worst available answer. Both now re-create
    the subscription on the new session, keep what was buffered, and say that
    events raised while it was down were not received.
    """
    from test_events_e2e import ALARM_SEVERITY, _trigger_alarm

    server = restartable_opcua_server
    async with connect(params_for(impl, server.url)) as session:
        subscribed = await session.call_tool("subscribe_events", {})
        assert not subscribed.is_error, text_of(subscribed)

        server.restart()
        for _ in range(10):
            status = await session.call_tool("get_server_status", {})
            if '"connected": true' in text_of(status):
                break
            await asyncio.sleep(1.0)

        await _trigger_alarm(session)
        texts, records = [], []
        for _ in range(10):
            result = await session.call_tool("read_events", {})
            assert not result.is_error, f"{impl}: {text_of(result)}"
            texts.append(text_of(result))
            # Structured, not text: the text carries the notice beside the records.
            records += result.structured_content["result"]
            if any(record["severity"] == ALARM_SEVERITY for record in records):
                break
            await asyncio.sleep(1.0)

    assert any(record["severity"] == ALARM_SEVERITY for record in records), (
        f"{impl}: the event subscription stopped delivering after the restart: {texts}"
    )
    gap = CONTRACT["notices"]["eventsResubscribed"]
    assert sum(gap in text for text in texts) == 1, (
        f"{impl}: the gap was not reported once: {texts}"
    )


async def test_a_bad_retry_setting_is_rejected_at_startup(impl):
    """Configuration errors are for the operator to see, not to guess at.

    Both runtimes refuse to start rather than quietly falling back to a default
    the operator did not ask for, so the session never initialises. Needs no OPC
    UA server: the check happens before anything is connected to.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    params = params_for(impl, "opc.tcp://127.0.0.1:1/unused", OPCUA_RECONNECT_MAX_RETRY="soon")
    # `BaseException`, because a server that exits during startup surfaces as
    # whatever the stdio transport's task group wraps the failure in — an
    # `ExceptionGroup`, not a `ToolError`. What is being asserted is only that
    # the session never comes up; the wording of the refusal is pinned by
    # `test_a_bad_setting_is_refused` in `tests/unit/test_reconnect.py`.
    with pytest.raises(BaseException):  # noqa: B017
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()


# --- the catalogue must not wait on the plant (issue #83) ----------------------


#: Long enough that a `tools/list` which still connects cannot possibly hide
#: inside the allowance below. With these settings a full round of backoff is
#: 2s + 4s + 8s + 8s = 22s on both runtimes, against an endpoint that refuses
#: immediately.
SLOW_RETRY = {
    "OPCUA_RECONNECT_INITIAL_DELAY_MS": "2000",
    "OPCUA_RECONNECT_MAX_DELAY_MS": "8000",
    "OPCUA_RECONNECT_MAX_RETRY": "3",
}

#: What two catalogue requests are allowed to cost with the plant unreachable.
#: An order of magnitude below the backoff budget above, below the 3s warm-up
#: window `tools/list` used to wait out (#140), and far above what answering from
#: the contract actually takes.
LIST_TOOLS_BUDGET_SECONDS = 2.0


async def test_listing_tools_does_not_wait_for_an_unreachable_server(impl):
    """`tools/list` used to pay the whole reconnect budget, per call.

    It opened a connection before probing capabilities, and `connect` holds its
    lock across the entire backoff loop — so against a plant that is down every
    catalogue request sat through it and serialised every concurrent tool call
    behind it. Clients list at session start, which is exactly when a plant that
    is down is most likely to be down.

    The startup warm-up is where the waiting now happens, once, and beside the
    requests rather than in front of them (#136) — and since #140 a catalogue
    request does not wait for it at all, not even for the bounded window it used
    to: the catalogue is the contract, so there is nothing for it to wait for.
    """
    params = _server_params(impl, "opc.tcp://127.0.0.1:1/unreachable")
    params.env.update(SLOW_RETRY)

    async with connect(params) as session:
        started = asyncio.get_running_loop().time()
        listed = await session.list_tools()
        # Twice: the second is the one that would re-probe under the old code.
        await session.list_tools()
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < LIST_TOOLS_BUDGET_SECONDS, (
        f"{impl}: two tools/list against an unreachable server took {elapsed:.1f}s; "
        f"the catalogue is waiting on the network again"
    )
    # The whole catalogue, not a reduced one: nothing in it depends on the plant.
    names = [tool.name for tool in listed.tools]
    assert names == [tool["name"] for tool in CONTRACT["tools"]], f"{impl}: {names}"


# --- MCP comes up whatever the plant is doing (issue #136) -------------------------


#: Retry forever, with a round long enough that a server still waiting on it
#: cannot hide inside the deadline below: 2s + 4s + 4s + 4s = 14s of backoff per
#: round on both runtimes, against an endpoint that refuses at once. Before #136
#: the Node server handed -1 to node-opcua as it was, and its first round — which
#: it awaited before opening the MCP transport — never ended.
OFFLINE_RETRY = {
    "OPCUA_RECONNECT_INITIAL_DELAY_MS": "2000",
    "OPCUA_RECONNECT_MAX_DELAY_MS": "4000",
    "OPCUA_RECONNECT_MAX_RETRY": "-1",
}

#: From spawning the process to having answered `initialize`, `tools/list` and
#: `get_server_status`. Covers interpreter start-up plus the shared 3s warm-up
#: window, and is well short of one round above, so a server that waits for the
#: round anywhere on that path cannot pass.
DIAGNOSABLE_WITHIN_SECONDS = 10.0


async def test_mcp_starts_and_stays_diagnosable_while_the_endpoint_is_offline(
    impl, restartable_opcua_server
):
    """Plant connectivity is runtime state; it must not gate the protocol.

    The MCP server is launched *before* its OPC UA server exists, with unlimited
    retries — the configuration an operator picks precisely because the plant
    may be down for a while. The client must get a working MCP server at once:
    `initialize`, a catalogue, and a status report that says what is wrong. A
    read has to fail as an outage, in the shared words, not hang. Then the plant
    comes up, and the same MCP session — never restarted — reads from it.
    """
    from test_diagnostics_e2e import status_of

    server = restartable_opcua_server
    server.stop()
    params = _server_params(impl, server.url)
    params.env.update(OFFLINE_RETRY)
    loop = asyncio.get_running_loop()

    began = loop.time()
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await asyncio.wait_for(session.initialize(), DIAGNOSABLE_WITHIN_SECONDS)
        listed = await asyncio.wait_for(session.list_tools(), DIAGNOSABLE_WITHIN_SECONDS)
        status = status_of(
            await asyncio.wait_for(
                session.call_tool("get_server_status", {}), DIAGNOSABLE_WITHIN_SECONDS
            )
        )
        diagnosable = loop.time() - began

        assert diagnosable < DIAGNOSABLE_WITHIN_SECONDS, (
            f"{impl}: initialize + tools/list + get_server_status took {diagnosable:.1f}s "
            f"against an endpoint that is down; the protocol is waiting on the plant again"
        )
        names = {tool.name for tool in listed.tools}
        assert {"get_server_status", "read_opcua_nodes"} <= names, f"{impl}: {sorted(names)}"
        assert status["connected"] is False, f"{impl}: {status}"
        assert status["endpoint_url"] == server.url
        assert status["error"], f"{impl}: a disconnected status must say why"

        # A read joins the connection round and fails when it does: bounded, and
        # worded as the outage it is. One round plus a generous margin.
        during = await asyncio.wait_for(
            session.call_tool("read_opcua_nodes", {"node_ids": [NODE["Temperature"]]}), 30
        )
        assert during.is_error, f"{impl}: a read against a server that is down succeeded"
        assert f"Not connected to the OPC UA server at {server.url}" in text_of(during), (
            f"{impl}: the outage was not reported as one: {text_of(during)}"
        )

        # The plant arrives. Same MCP session, no restart.
        server.start()
        after = await read_until_ok(session, NODE["Temperature"], attempts=30)
        assert not after.is_error, f"{impl}: never recovered: {text_of(after)}"
        assert records_of(after)[0]["status"] == "Good", text_of(after)
        assert status_of(await session.call_tool("get_server_status", {}))["connected"] is True


# --- the catalogue does not move with the plant (issue #140) -----------------------


async def _catalogue(params) -> str:
    async with connect(params) as session:
        return normalized_catalogue(await session.list_tools())


async def test_offline_online_and_capability_less_startups_advertise_one_catalogue(
    impl, restartable_opcua_server
):
    """Names and schemas, compared whole: down, up, and up without history.

    Before #140 these were three catalogues. Offline had neither history tool,
    the bundled mock had both but no `aggregate_function`, and a server without
    history had neither again — so what a client was told depended on when it
    listed, and a client that listed once kept whatever that was.
    """
    server = restartable_opcua_server
    online = await _catalogue(params_for(impl, server.url))
    server.restart("--no-history")
    without_history = await _catalogue(params_for(impl, server.url))
    server.stop()
    params = _server_params(impl, server.url)
    params.env.update(SLOW_RETRY)
    offline = await _catalogue(params)

    assert online == offline, f"{impl}: the catalogue changed with the plant offline"
    assert online == without_history, f"{impl}: the catalogue changed with the server's features"


async def test_a_client_that_lists_once_can_use_everything_once_the_plant_is_back(
    impl, restartable_opcua_server
):
    """The client this issue is about: it lists at session start and never again.

    It starts while the plant is down, so under the old catalogue it would have
    been told there is no history tool and never learned otherwise — no portable
    notification exists to tell it. Now it is told everything at once; a history
    read while the plant is down is refused as the outage it is; and once the
    plant is back, the tools from that one list simply work.
    """
    server = restartable_opcua_server
    server.stop()
    async with connect(params_for(impl, server.url)) as session:
        listed = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert {"read_opcua_history", "read_event_history"} <= set(listed), f"{impl}: {listed}"
        assert "aggregate_function" in listed["read_opcua_history"].input_schema["properties"]

        during = await asyncio.wait_for(
            session.call_tool("read_opcua_history", {"node_id": NODE["Temperature"]}), 30
        )
        assert during.is_error, f"{impl}: a history read against a server that is down succeeded"
        assert text_of(during).startswith("endpoint_offline: "), (
            f"{impl}: an outage must not read as a missing capability: {text_of(during)}"
        )

        server.start()
        history = None
        for _ in range(30):
            history = await session.call_tool(
                "read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 3}
            )
            if not history.is_error:
                break
            await asyncio.sleep(1.0)
        assert not history.is_error, f"{impl}: never recovered: {text_of(history)}"
        events = await session.call_tool("read_event_history", {"num_values": 3})
        assert not events.is_error, f"{impl}: {text_of(events)}"


async def _status_capabilities(session) -> dict:
    status = await session.call_tool("get_server_status", {})
    return status.structured_content["result"]["capabilities"]


async def _history_until(session, done, attempts: int = 30):
    """Call `read_opcua_history` until `done(result)`, riding out the restart.

    A call that lands mid-restart may fail as the outage, or be served by the
    old answers before the new session has been noticed. What is promised is what
    the next calls say once it has, which is what this waits for.
    """
    result = None
    for _ in range(attempts):
        result = await session.call_tool(
            "read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 3}
        )
        if done(result):
            return result
        await asyncio.sleep(1.0)
    return result


async def test_a_restart_with_different_capabilities_invalidates_the_answers(
    impl, restartable_opcua_server
):
    """The answers belong to a session, and a restarted server may not be the same one.

    The server comes back without history, and then with it again, under one
    MCP session that never lists again. Each time the next calls must be decided
    by what the *new* server says — a stale yes would send a request the server
    cannot serve, a stale no would refuse one it can — and `get_server_status`
    must say which session its answers are from.
    """
    server = restartable_opcua_server

    def refused(result) -> bool:
        return result.is_error and text_of(result).startswith("capability_not_supported: ")

    async with connect(params_for(impl, server.url)) as session:
        first = await session.call_tool(
            "read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 3}
        )
        assert not first.is_error, f"{impl}: {text_of(first)}"
        before = await _status_capabilities(session)
        assert before["support"]["history"] == "supported", f"{impl}: {before}"

        server.restart("--no-history")
        without = await _history_until(session, refused)
        assert refused(without), f"{impl}: never refused as unsupported: {text_of(without)}"
        assert "historical data access (AccessHistoryDataCapability" in text_of(without)
        after = await _status_capabilities(session)
        assert after["support"]["history"] == "not_supported", f"{impl}: {after}"
        assert after["session_generation"] > before["session_generation"], (
            f"{impl}: the answers did not move to a new session: {before} -> {after}"
        )

        server.restart()
        back = await _history_until(session, lambda result: not result.is_error)
        assert not back.is_error, f"{impl}: a stale no outlived the restart: {text_of(back)}"
        again = await _status_capabilities(session)
        assert again["support"]["history"] == "supported", f"{impl}: {again}"
        assert again["session_generation"] > after["session_generation"], f"{impl}: {again}"


async def test_a_call_that_dies_mid_request_is_recognised_and_recovered(
    impl, restartable_opcua_server
):
    """The one test that can tell reconnection has actually stopped working.

    Everything else here restarts the server *between* calls, so by the time the
    next call arrives the client has already noticed and simply reconnects. This
    takes the plant away while the session still looks alive, so the failure
    arrives from inside a request, worded by the client library. That is the case
    `is_connection_error` exists for, and the one the old parametrized marker test
    could never produce: it asserted the classifier against its own constant, so a
    library rewording a message would have left it green and the server dead until
    restarted (issue #112).

    The two runtimes reach it by different paths, and the assertion says so rather
    than picking one. Python owns the whole of recovery — python-opcua has none —
    so the failure surfaces inside the request and the dispatcher's own "failed on
    a dead session" line is what proves the classification fired. node-opcua
    repairs its own channel and notices the socket first, so on that runtime the
    next call is usually refused by `ensureConnection` before it is dispatched,
    and the evidence is the library's own. `docs/architecture.md` draws exactly
    this distinction; a test that demanded one shape would be asserting a
    coincidence.

    What both must do is the part that matters: say the connection is gone rather
    than return something wrong, and work again afterwards without a restart.
    """
    server = restartable_opcua_server
    with tempfile.TemporaryFile("w+", errors="replace") as errlog:
        async with (
            stdio_client(params_for(impl, server.url), errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            before = await session.call_tool(
                "read_opcua_nodes", {"node_ids": [NODE["Temperature"]]}
            )
            assert not before.is_error, f"{impl}: {text_of(before)}"

            # The plant goes away with the session still looking alive.
            server.stop()
            during = await session.call_tool(
                "read_opcua_nodes", {"node_ids": [NODE["Temperature"]]}
            )
            assert during.is_error, f"{impl}: a read against a dead server succeeded"
            # And it says what is wrong. A read that came back as a puzzling tool
            # error, or worse as a stale value, is the failure mode this whole
            # layer exists to prevent.
            assert "Not connected to the OPC UA server" in text_of(during), (
                f"{impl}: the outage was not reported as one: {text_of(during)}"
            )

            server.start()
            after = await read_until_ok(session, NODE["Temperature"])
            assert not after.is_error, f"{impl}: never recovered: {text_of(after)}"
            assert records_of(after)[0]["status"] == "Good", text_of(after)

        errlog.seek(0)
        log = errlog.read()

    noticed = (
        # The Python dispatcher's own classification, which is the whole of
        # recovery on that runtime.
        "failed on a dead session" in log
        # node-opcua's, which gets there first on this one.
        or "connection lost" in log
        or "Failed to connect to OPC UA server" in log
    )
    assert noticed, (
        f"{impl}: nothing in the server's log says it noticed the connection had "
        f"gone, so whatever recovered did not do it on the outage's account:\n{log[-4000:]}"
    )


# --- stopped by a signal ------------------------------------------------------------
#
# What a supervisor, a container runtime or a Ctrl-C sends. The Python runtime had
# no handler at all (#157): SIGTERM killed it where it stood, leaving every
# subscription on the OPC UA server to publish into the void for its whole
# lifetime. Both now drop the subscriptions, close the session and exit 0 within
# a bounded grace period.
#
# POSIX only, and by definition rather than by skip: Windows has no SIGTERM to
# deliver to a child, and its console-control events are a different mechanism
# that neither runtime claims to handle.

#: The runtimes' own bound (`SHUTDOWN_GRACE_MS` / `SHUTDOWN_GRACE_SECONDS`) plus
#: room for the process to start answering and to be reaped.
SIGNAL_EXIT_BUDGET_SECONDS = 15


def _serving_argv(impl: str) -> list[str]:
    """The server as its own process, not behind `uv run`, so the signal reaches it."""
    if impl == "python":
        return [sys.executable, "-m", "opcua_mcp_server"]
    return ["node", str(NODE_BUILD)]


async def _send(proc, message: dict) -> None:
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()


async def _answer(proc, request_id: int) -> dict:
    """The JSON-RPC response to `request_id`, skipping anything else on stdout."""
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
        assert line, "the server closed stdout before answering"
        message = json.loads(line)
        if message.get("id") == request_id:
            return message


if os.name == "posix":

    @pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
    async def test_a_signal_drops_the_subscriptions_and_exits_cleanly(
        impl, signame, opcua_server, tmp_path
    ):
        """Exit 0, promptly, with an active subscription and stdin still open.

        stdin stays open on purpose: closing it is the other way a session ends,
        and a test that closed it could pass on that path alone.
        """
        stderr_path = tmp_path / "stderr.log"
        with stderr_path.open("w") as stderr:
            proc = await asyncio.create_subprocess_exec(
                *_serving_argv(impl),
                env=params_for(impl, opcua_server).env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
            try:
                await _send(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "signal-test", "version": "0"},
                        },
                    },
                )
                await _answer(proc, 1)
                await _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                await _send(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "subscribe_opcua_nodes",
                            "arguments": {"node_ids": [NODE["Temperature"]]},
                        },
                    },
                )
                subscribed = await _answer(proc, 2)
                assert not subscribed["result"].get("isError"), f"{impl}: {subscribed}"

                proc.send_signal(getattr(signal, signame))
                started = asyncio.get_running_loop().time()
                code = await asyncio.wait_for(proc.wait(), timeout=SIGNAL_EXIT_BUDGET_SECONDS)
                elapsed = asyncio.get_running_loop().time() - started
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        log = stderr_path.read_text(errors="replace")
        assert code == 0, f"{impl}: {signame} exited {code}:\n{log[-4000:]}"
        assert "Traceback" not in log, f"{impl}: {signame}:\n{log[-4000:]}"
        assert elapsed < SIGNAL_EXIT_BUDGET_SECONDS, f"{impl}: took {elapsed:.1f}s"
