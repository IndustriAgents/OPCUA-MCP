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

import pytest
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

#: What a catalogue request is allowed to cost with the plant unreachable. Two
#: orders of magnitude below the backoff budget above, and far above what
#: answering from the contract actually takes.
LIST_TOOLS_BUDGET_SECONDS = 5.0


async def test_listing_tools_does_not_wait_for_an_unreachable_server(impl):
    """`tools/list` used to pay the whole reconnect budget, per call.

    It opened a connection before probing capabilities, and `connect` holds its
    lock across the entire backoff loop — so against a plant that is down every
    catalogue request sat through it and serialised every concurrent tool call
    behind it. Clients list at session start, which is exactly when a plant that
    is down is most likely to be down.

    The startup warm-up is where the waiting now happens, once, and it is not on
    this path: by the time a request is served the attempt has already been made
    and given up on.
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
    # Still a usable catalogue: the core tools do not depend on a connection.
    names = {tool.name for tool in listed.tools}
    assert "get_server_status" in names, f"{impl}: {sorted(names)}"
    assert "read_opcua_nodes" in names, f"{impl}: {sorted(names)}"


async def test_the_catalogue_gains_the_optional_tools_once_the_server_is_reachable(
    impl, restartable_opcua_server
):
    """Convergence without a notification, which is what both runtimes promise.

    No `notifications/tools/list_changed` is sent — see docs/architecture.md for
    why neither runtime offers one. What is promised instead is that the
    catalogue is *correct when asked*: the capabilities are re-probed on every
    (re)connect, so a client that listed too early and lists again gets the whole
    surface.
    """
    async with connect(params_for(impl, restartable_opcua_server.url)) as session:
        listed = await session.list_tools()
        assert "read_opcua_history" in {tool.name for tool in listed.tools}, (
            f"{impl}: the mock advertises HistoricalAccess, so the history tool must be offered"
        )
