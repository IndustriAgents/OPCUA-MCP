"""End-to-end tests for `get_server_status` (issue #13).

The health/diagnostics tool answers "are we connected, to what, and is the
server healthy?" — so the things worth asserting are that it reaches the real
server (its clock, its build info, its namespace array are the server's own, not
this process's idea of them), that both runtimes report them identically, and
that it still answers when there is nothing to report because the connection is
down.

Run:
    cd tests && uv run --no-sync pytest e2e/test_diagnostics_e2e.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ROOT
from test_contract_parity import assert_matches_result_shape
from test_mcp_e2e import ISO_UTC_TIMESTAMP, NODE_BUILD, _server_params, connect, text_of

#: Every namespace array begins with the OPC UA namespace itself, at index 0.
OPC_UA_NAMESPACE = "http://opcfoundation.org/UA/"

BUILD_INFO_FIELDS = {
    "product_name",
    "product_uri",
    "manufacturer_name",
    "software_version",
    "build_number",
    "build_date",
}


def status_of(result) -> dict:
    """The single status record, from the text block and the structured result.

    Asserts the two agree, because a client may read either: the contract says
    `get_server_status` returns one object as one text block and the same object
    as `{"result": ...}`.
    """
    assert not result.is_error, text_of(result)
    assert len(result.content) == 1, f"expected one content block, got {len(result.content)}"
    status = json.loads(result.content[0].text)
    assert result.structured_content == {"result": status}, (
        "structured status differs from the compatibility text block"
    )
    return status


@pytest.fixture(params=["python", "node"])
def impl_params(request, opcua_server):
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, opcua_server)


async def test_reports_a_live_connection(impl_params):
    impl, params = impl_params
    async with connect(params) as session:
        result = await session.call_tool("get_server_status", {})

    status = status_of(result)
    assert_matches_result_shape([status], "serverStatus", f"{impl}/get_server_status")

    assert status["connected"] is True
    assert status["error"] is None
    # The endpoint is the one the test pointed this server at, not a default.
    assert status["endpoint_url"] == params.env["OPCUA_SERVER_URL"]
    assert status["security"] == "policy=None mode=None user=anonymous"


async def test_reports_the_servers_own_status(impl_params):
    """State, clock and start time come from the OPC UA server, not from here."""
    impl, params = impl_params
    before = datetime.now(timezone.utc)
    async with connect(params) as session:
        result = await session.call_tool("get_server_status", {})
    after = datetime.now(timezone.utc)

    status = status_of(result)
    assert status["server_state"] == "Running", f"{impl}: mock server is not Running"

    for field in ("current_time", "start_time"):
        assert ISO_UTC_TIMESTAMP.match(status[field]), (
            f"{impl}: {field}={status[field]!r} is not ISO-8601 UTC"
        )

    # The server's clock is the mock's own, on this machine, so it must fall
    # inside the window the call was made in — give or take a generous slack for
    # a slow CI box. This is what distinguishes a real read from a stub.
    current = datetime.fromisoformat(status["current_time"].replace("Z", "+00:00"))
    assert before - timedelta(seconds=30) <= current <= after + timedelta(seconds=30), (
        f"{impl}: current_time {current} is outside the call window [{before}, {after}]"
    )

    # The mock was started by this session's fixture, so it cannot have been
    # running for longer than the session has.
    start = datetime.fromisoformat(status["start_time"].replace("Z", "+00:00"))
    assert start <= current, f"{impl}: start_time {start} is after current_time {current}"


async def test_reports_the_servers_build_info(impl_params):
    impl, params = impl_params
    async with connect(params) as session:
        result = await session.call_tool("get_server_status", {})

    build = status_of(result)["build_info"]
    assert set(build) == BUILD_INFO_FIELDS, f"{impl}: build_info fields {sorted(build)}"
    # python-opcua's server identifies itself; the value is the mock's, which is
    # what makes this an assertion about a real read rather than about our code.
    assert build["manufacturer_name"] == "FreeOpcUa"
    assert "FreeOpcUa" in build["product_name"]
    assert build["product_uri"], f"{impl}: empty product_uri"


async def test_reports_the_namespace_array(impl_params):
    """Namespace index -> URI, which is what makes an 'ns=2;i=3' node ID resolvable."""
    impl, params = impl_params
    async with connect(params) as session:
        result = await session.call_tool("get_server_status", {})

    namespaces = status_of(result)["namespaces"]
    assert namespaces, f"{impl}: no namespaces reported"
    assert [entry["index"] for entry in namespaces] == list(range(len(namespaces))), (
        f"{impl}: namespace indexes are not 0..n: {namespaces}"
    )
    assert namespaces[0]["uri"] == OPC_UA_NAMESPACE
    # The mock registers its own namespace, which is where its nodes live.
    assert any("freeopcua" in entry["uri"] for entry in namespaces[1:]), namespaces


async def test_both_servers_report_the_same_thing(opcua_server):
    """The point of the shared shape: one report, two runtimes, same reading.

    Everything that comes from the OPC UA server itself must match exactly. The
    three fields that legitimately differ — the moment of the call and the
    sub-second precision each client library decodes a timestamp with — are
    compared as shapes, not values.
    """
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    reports = {}
    for impl in ("python", "node"):
        async with connect(_server_params(impl, opcua_server)) as session:
            reports[impl] = status_of(await session.call_tool("get_server_status", {}))

    moving = {"current_time", "start_time", "build_info"}
    assert {k: v for k, v in reports["python"].items() if k not in moving} == {
        k: v for k, v in reports["node"].items() if k not in moving
    }

    # start_time is the same instant read twice; the runtimes differ only in how
    # many sub-second digits they keep (microseconds from Python, milliseconds
    # from Node), so compare to the second.
    assert reports["python"]["start_time"][:19] == reports["node"]["start_time"][:19]

    python_build = dict(reports["python"]["build_info"])
    node_build = dict(reports["node"]["build_info"])
    assert python_build.pop("build_date")[:19] == node_build.pop("build_date")[:19]
    assert python_build == node_build


async def test_answers_when_the_server_is_unreachable(opcua_server):
    """The one tool that must not fail for being disconnected — it reports it.

    Pointed at a port nothing is listening on, so the failure is a refused
    connection rather than a slow one. Everything knowable without a server —
    the endpoint and the security in force — is still reported, and `error` says
    what went wrong.
    """
    from conftest import HOST, _free_port

    dead_url = f"opc.tcp://{HOST}:{_free_port()}/nothing/here"

    for impl in ("python", "node"):
        if impl == "node" and not NODE_BUILD.exists():
            continue
        params = _server_params(impl, dead_url)
        # Fail fast: with no server to find, the point is the report, not the
        # retry schedule. The defaults would spend several seconds backing off.
        params.env.update(
            {
                "OPCUA_RECONNECT_MAX_RETRY": "0",
                "OPCUA_RECONNECT_INITIAL_DELAY_MS": "10",
            }
        )
        async with connect(params) as session:
            result = await session.call_tool("get_server_status", {})

        status = status_of(result)
        assert_matches_result_shape([status], "serverStatus", f"{impl}/get_server_status")
        assert status["connected"] is False, f"{impl}: claims to be connected to {dead_url}"
        assert status["endpoint_url"] == dead_url
        assert status["security"] == "policy=None mode=None user=anonymous"
        assert status["server_state"] is None
        assert status["namespaces"] == []
        assert status["error"], f"{impl}: reported no reason for being disconnected"


async def test_other_tools_say_what_to_call_when_disconnected():
    """A tool that cannot run should point at the one that explains why.

    Both runtimes word this identically, so an agent that has learned to follow
    the hint on one server follows it on the other.
    """
    from conftest import HOST, _free_port
    from opcua_mcp_server.connection import not_connected_message

    dead_url = f"opc.tcp://{HOST}:{_free_port()}/nothing/here"

    for impl in ("python", "node"):
        if impl == "node" and not NODE_BUILD.exists():
            continue
        params = _server_params(impl, dead_url)
        params.env.update(
            {
                "OPCUA_RECONNECT_MAX_RETRY": "0",
                "OPCUA_RECONNECT_INITIAL_DELAY_MS": "10",
            }
        )
        async with connect(params) as session:
            result = await session.call_tool("read_opcua_nodes", {"node_ids": ["ns=2;i=3"]})

        assert result.is_error, f"{impl}: a read against a dead server reported success"
        text = text_of(result)
        assert f"Not connected to the OPC UA server at {dead_url}" in text, text
        assert "Call get_server_status for details." in text, text
        # The same sentence both runtimes build from the shared helper.
        assert not_connected_message(dead_url, "x").split(":")[0] in text


def test_the_contract_is_where_the_node_ids_come_from():
    """Neither server may carry its own copy of ServerStatus / NamespaceArray."""
    contract = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))
    diagnostics = contract["diagnostics"]
    assert diagnostics["serverStatusNodeId"] == "ns=0;i=2256"
    assert diagnostics["namespaceArrayNodeId"] == "ns=0;i=2255"

    from opcua_mcp_server.contract import NAMESPACE_ARRAY_NODE_ID, SERVER_STATUS_NODE_ID

    assert diagnostics["serverStatusNodeId"] == SERVER_STATUS_NODE_ID
    assert diagnostics["namespaceArrayNodeId"] == NAMESPACE_ARRAY_NODE_ID


# --- the server's own diagnostics (#121) ------------------------------------------

DIAGNOSTICS_FIELDS = {
    "server_view_count",
    "current_session_count",
    "cumulated_session_count",
    "security_rejected_session_count",
    "rejected_session_count",
    "session_timeout_count",
    "session_abort_count",
    "current_subscription_count",
    "cumulated_subscription_count",
    "publishing_interval_count",
    "security_rejected_requests_count",
    "rejected_requests_count",
}


@pytest.fixture(params=["python", "node"])
def aggregate_impl_params(request, aggregate_opcua_server):
    """The mock that actually populates ServerDiagnosticsSummary.

    The bundled Python mock creates the node and leaves it empty — python-opcua
    does not fill it in — so the two mocks cover the two branches this field has,
    and neither branch is a guess.
    """
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, aggregate_opcua_server)


async def test_reports_the_servers_own_diagnostics(aggregate_impl_params):
    """ "Why is this slow, am I being rejected, how many sessions are open?"

    Questions people actually ask an assistant about a server they cannot see,
    and the answers were sitting in a standard node nothing read.
    """
    impl, params = aggregate_impl_params
    async with connect(params) as session:
        result = await session.call_tool("get_server_status", {})

    diagnostics = status_of(result)["diagnostics"]
    assert diagnostics is not None, f"{impl}: the aggregate mock does publish diagnostics"
    assert set(diagnostics) == DIAGNOSTICS_FIELDS, f"{impl}: {sorted(diagnostics)}"
    assert all(isinstance(value, int) for value in diagnostics.values()), diagnostics
    # This connection is one of them, so the server is holding at least one
    # session and has opened at least one since it started.
    assert diagnostics["current_session_count"] >= 1, f"{impl}: {diagnostics}"
    assert diagnostics["cumulated_session_count"] >= diagnostics["current_session_count"], (
        f"{impl}: cumulated cannot be below current: {diagnostics}"
    )


async def test_a_server_that_publishes_no_diagnostics_says_null(impl_params):
    """Part 5 lets a server leave diagnostics off, so null is an answer.

    The bundled mock is exactly that case: python-opcua creates
    ServerDiagnosticsSummary and never populates it. Reporting a record of zeroes
    would be inventing twelve numbers; reporting null says "this server does not
    tell me", which is the truth and is what a reviewer needs.
    """
    impl, params = impl_params
    async with connect(params) as session:
        result = await session.call_tool("get_server_status", {})

    status = status_of(result)
    assert status["diagnostics"] is None, f"{impl}: {status['diagnostics']}"
    # And the rest of the report is unaffected — a missing optional must not
    # cost the fields that are there.
    assert status["connected"] is True, impl
    assert status["server_state"] == "Running", impl


async def test_a_disconnected_server_reports_no_diagnostics(opcua_server):
    """Nothing to ask, so nothing to report — and still not an error.

    `get_server_status` is the one tool that must answer while the connection is
    down, so a diagnostics read that cannot happen must not change that.
    """
    unreachable = "opc.tcp://127.0.0.1:1/none"
    for impl in ("python", "node"):
        if impl == "node" and not NODE_BUILD.exists():
            continue
        params = _server_params(impl, unreachable)
        params.env["OPCUA_RECONNECT_MAX_RETRY"] = "0"
        async with connect(params) as session:
            result = await session.call_tool("get_server_status", {})

        status = status_of(result)
        assert status["connected"] is False, impl
        assert status["diagnostics"] is None, f"{impl}: {status['diagnostics']}"
        assert status["error"], f"{impl}: a disconnected status must say why"


async def test_both_runtimes_report_the_same_diagnostics(aggregate_opcua_server):
    """Twelve counters in one order, or it is two records rather than one shape."""
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    shapes = {}
    for impl in ("python", "node"):
        async with connect(_server_params(impl, aggregate_opcua_server)) as session:
            diagnostics = status_of(await session.call_tool("get_server_status", {}))["diagnostics"]
        assert diagnostics is not None, impl
        shapes[impl] = list(diagnostics)

    assert shapes["python"] == shapes["node"], (
        f"the two runtimes order the counters differently:\n"
        f"  python: {shapes['python']}\n  node:   {shapes['node']}"
    )
