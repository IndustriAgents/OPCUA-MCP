"""Exercise the real Python SDK boundary, including audit normalization."""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config
from opcua_mcp_server.server import create_server
from opcua_mcp_server.state import ServerState

FIXTURE = json.loads(
    (ROOT / "tests/fixtures/unexpected-tool-errors.json").read_text(encoding="utf-8")
)
OPERATOR = {
    "OPCUA_PROFILE": "operator",
    "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=5",
    "OPCUA_ALLOW_INSECURE_CONTROL": "true",
    "OPCUA_ENFORCE_EU_RANGE": "false",
}


def server_with_failure(case, anticipated=False):
    state = ServerState(policy=ToolPolicy(parse_policy_config(OPERATOR)))
    server = create_server(state)
    server.remove_tool(case["tool"])

    def fail():
        if anticipated:
            raise ToolError(FIXTURE["anticipated"])
        raise RuntimeError(case["internal"])

    if case["tool"] == "read_opcua_nodes":

        async def broken(node_ids: list[str]):
            fail()
    elif case["tool"] == "write_opcua_nodes":

        async def broken(nodes: list[dict]):
            fail()
    else:

        async def broken():
            fail()

    server.tool(name=case["tool"])(broken)
    return server


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["tool"])
async def test_crashes_expose_only_the_tool_name(case, capsys):
    server = server_with_failure(case)
    with pytest.raises(UnexpectedToolError) as raised:
        await server.call_tool(case["tool"], case["arguments"])
    assert str(raised.value) == case["public"]
    assert case["internal"] not in str(raised.value)
    assert raised.value.__cause__ is not None
    records = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"event":"opcua_mcp_policy"')
    ]
    if case["tool"] == "write_opcua_nodes":
        assert [record["decision"] for record in records] == ["allowed", "failed"]
        assert records[-1]["reason"] == case["public"]


async def test_anticipated_failures_keep_their_own_message():
    case = FIXTURE["cases"][0]
    server = server_with_failure(case, anticipated=True)
    with pytest.raises(ToolError) as raised:
        await server.call_tool(case["tool"], case["arguments"])
    assert str(raised.value) == FIXTURE["anticipated"]
