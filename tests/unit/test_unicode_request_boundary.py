"""Malformed Unicode reaches neither the OPC UA connection nor a feature handler."""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua_mcp_server.server import create_server

CASES = [
    case
    for case in json.loads(
        (ROOT / "tests/fixtures/request-limits.json").read_text(encoding="utf-8")
    )["requests"]
    if "Unicode surrogate" in (case.get("error") or "")
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_real_pipeline_refuses_before_invocation(case):
    server = create_server()
    server.remove_tool("write_opcua_nodes")
    calls = []

    async def unexpected(nodes: list[dict]):
        calls.append(nodes)
        raise AssertionError("must not invoke a feature")

    server.tool(name="write_opcua_nodes")(unexpected)
    with pytest.raises(ToolError) as raised:
        await server.call_tool(case["tool"], case["arguments"])
    assert str(raised.value) == case["error"]
    assert calls == []
