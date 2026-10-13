"""Actual alarm handlers retain the calling tool's schema and frame, with one native call."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua import ua
from opcua_mcp_server import events, server
from opcua_mcp_server.errors import message

CASES = json.loads((ROOT / "tests/fixtures/alarm-tool-alias.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_actual_alarm_handler(case, monkeypatch):
    calls = []

    def call(requests):
        calls.extend(requests)
        if "error" in case:
            raise ValueError("native refused")
        return [SimpleNamespace(StatusCode=ua.StatusCode(getattr(ua.StatusCodes, case["status"])))]

    node = SimpleNamespace(nodeid=ua.NodeId(7, 2), server=SimpleNamespace(call=call))
    client = SimpleNamespace(get_node=lambda _: node)
    monkeypatch.setattr(
        events, "_action_method", lambda *_: SimpleNamespace(nodeid=ua.NodeId(9111))
    )
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"opcua_client": client})
    )

    async def invoke():
        return await getattr(server, case["tool"])(ctx=ctx, **case["arguments"])

    if "error" in case:
        with pytest.raises(ToolError) as raised:
            await invoke()
        assert str(raised.value) == message(case["error"], **case["fields"])
    else:
        assert (await invoke()).structured_content["result"] == case["expected"]
    assert len(calls) == 1
    assert calls[0].ObjectId == ua.NodeId(7, 2)
    assert calls[0].InputArguments[0].Value == b"\x01"
    assert calls[0].InputArguments[1].Value.Text == case["arguments"]["comment"]
