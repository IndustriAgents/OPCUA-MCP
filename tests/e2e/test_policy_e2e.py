"""End-to-end checks for the deployment policy boundary."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_e2e import NODE_BUILD, ROOT, connect, text_of

POLICY_ENV = {
    "OPCUA_PROFILE",
    "OPCUA_POLICY_FILE",
    "OPCUA_ALLOWED_TOOLS",
    "OPCUA_ALLOWED_WRITE_NODES",
    "OPCUA_ALLOWED_METHODS",
    "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS",
    "OPCUA_ALLOW_INSECURE_CONTROL",
}

REQUIRED_OBSERVE_TOOLS = {
    "read_opcua_nodes",
    "browse_opcua_nodes",
    # Diagnostics belong in the most restricted profile there is: an
    # observe-only deployment is exactly where "is this thing even connected?"
    # has to be answerable.
    "get_server_status",
    "subscribe_opcua_nodes",
    "list_subscriptions",
    "unsubscribe_opcua_nodes",
    "subscribe_events",
    "read_events",
    "list_active_alarms",
}

OPTIONAL_OBSERVE_TOOLS = {"read_opcua_history"}


def observe_params(impl: str, url: str) -> StdioServerParameters:
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env["OPCUA_SERVER_URL"] = url
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


def operator_params(impl: str, url: str) -> StdioServerParameters:
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env.update(
        {
            "OPCUA_SERVER_URL": url,
            "OPCUA_PROFILE": "operator",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13",
        }
    )
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


@pytest.fixture(params=["python", "node"])
def observe_server(request, opcua_server):
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return request.param, observe_params(request.param, opcua_server)


async def test_default_profile_advertises_only_observe_tools(observe_server):
    impl, params = observe_server
    async with connect(params) as session:
        response = await session.list_tools()

    tools = {tool.name: tool for tool in response.tools}
    assert set(tools) >= REQUIRED_OBSERVE_TOOLS, impl
    assert set(tools) <= REQUIRED_OBSERVE_TOOLS | OPTIONAL_OBSERVE_TOOLS, impl
    assert tools["read_opcua_nodes"].annotations.read_only_hint is True
    assert tools["subscribe_opcua_nodes"].annotations.read_only_hint is False
    assert all(tool.annotations.destructive_hint is False for tool in tools.values())


async def test_hidden_control_tool_is_still_rejected_when_called_directly(observe_server):
    impl, params = observe_server
    async with connect(params) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": "ns=2;i=2", "value": 999}]},
        )

    assert result.is_error is True, impl
    assert "disabled by OPCUA_PROFILE=observe" in text_of(result)


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_operator_profile_exposes_and_enforces_only_configured_targets(impl, opcua_server):
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect(operator_params(impl, opcua_server)) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        allowed = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
        )
        denied = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
        )

    assert "write_opcua_nodes" in names, impl
    assert "call_opcua_method" not in names, impl
    assert not allowed.is_error, text_of(allowed)
    assert denied.is_error is True, impl
    assert "not writable under the operator policy" in text_of(denied)


# --- the control audit trail -----------------------------------------------------


@asynccontextmanager
async def connect_capturing_stderr(params: StdioServerParameters):
    """An MCP session whose server stderr is collected — where the audit trail goes."""
    with tempfile.TemporaryFile("w+", errors="replace") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session, errlog
        errlog.seek(0)


def audit_records(errlog) -> list[dict]:
    """Every `opcua_mcp_policy` line the server wrote, parsed."""
    errlog.seek(0)
    records = []
    for line in errlog.read().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "opcua_mcp_policy":
            records.append(record)
    return records


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_audit_trail_records_what_a_control_call_targeted(impl, opcua_server):
    """An audit record that says a write was permitted but not *what* was written
    is not an audit trail.

    This had no test, which is how it broke: `_audit_targets` switched on tool
    *names*, so renaming the tools in 0.4.0 left every write logging
    `decision: "allowed"` with no `node_ids` at all — silently, because nothing
    looked. The targets now come from the same `guard` declaration the policy
    authorises from, so the two cannot disagree about which arguments matter.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        allowed = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
        )
        assert not allowed.is_error, text_of(allowed)
        denied = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
        )
        assert denied.is_error, impl
        records = audit_records(errlog)

    by_decision = {}
    for record in records:
        by_decision.setdefault(record["decision"], []).append(record)

    assert "allowed" in by_decision, f"{impl}: nothing recorded as allowed: {records}"
    assert "denied" in by_decision, f"{impl}: nothing recorded as denied: {records}"
    # The outcome, not only the decision: "permitted" and "happened" are
    # different facts, and the gap between them is where a control call that
    # reached the plant and then failed lives.
    assert "completed" in by_decision, f"{impl}: no outcome recorded: {records}"

    for decision in ("allowed", "completed"):
        [record] = by_decision[decision]
        assert record["tool"] == "write_opcua_nodes", record
        assert record["profile"] == "operator", record
        assert record["node_ids"] == ["ns=2;i=13"], f"{impl}: targets missing from {record}"

    [refusal] = by_decision["denied"]
    assert refusal["node_ids"] == ["ns=2;i=12"], f"{impl}: targets missing from {refusal}"
    assert "not writable under the operator policy" in refusal["reason"], refusal


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_audit_trail_never_records_the_value_written(impl, opcua_server):
    """Targets, never process data.

    A setpoint is what the plant is doing, and this stream is the one an MCP
    client shows the user and a log collector ships off the machine.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "31.25"}]}
        )
        records = audit_records(errlog)

    assert records, f"{impl}: nothing was audited at all"
    for record in records:
        assert "31.25" not in json.dumps(record), f"{impl}: the written value leaked: {record}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_reads_are_not_audited(impl, opcua_server):
    """An audit trail that recorded every read would bury the lines anyone wants."""
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        await session.call_tool("read_opcua_nodes", {"node_ids": ["ns=2;i=3"]})
        await session.call_tool("browse_opcua_nodes", {})
        records = audit_records(errlog)

    assert records == [], f"{impl}: a read was audited: {records}"
