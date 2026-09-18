"""End-to-end checks for the deployment policy boundary."""

from __future__ import annotations

import os

import pytest
from mcp import StdioServerParameters
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
