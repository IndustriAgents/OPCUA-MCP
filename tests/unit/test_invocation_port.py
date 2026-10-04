"""Contract-directed recovery with no native SDK or MCP transport."""

from __future__ import annotations

import copy
import json

import pytest
from conftest import ROOT
from opcua_mcp_server.application.execution import ExecutionCall
from opcua_mcp_server.application.invocation import invoke_tool
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.errors import ApplicationRefusal, message

CASES = json.loads((ROOT / "tests/fixtures/invocation-port.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_invocation_port(case):
    before = copy.deepcopy(case)
    calls = []
    native = TimeoutError("native timeout")

    def record(name):
        calls.append(name)
        if case.get("crash") == name:
            raise (
                native
                if name in ("connect", "reconnect:old-session")
                else ApplicationRefusal("test refusal")
            )

    call = ExecutionCall(
        case["tool"], {}, next(t for t in CONTRACT["tools"] if t["name"] == case["tool"]), "id"
    )

    class Port:
        caps = 0
        dispatches = 0

        def endpoint(self):
            return "opc.tcp://test:4840"

        def session(self):
            record("session")
            return "old-session"

        def has_connection(self):
            return True

        async def wait_for_warm_up(self):
            record("warm-up")

        async def connect(self):
            record("connect")

        async def capabilities(self, call):
            self.caps += 1
            record(f"capabilities:{self.caps}")

        async def dispatch(self, call):
            self.dispatches += 1
            record(f"dispatch:{self.dispatches}")
            if case["tool"] != "get_server_status" and self.dispatches == 1:
                raise native
            if case.get("secondFailure"):
                raise RuntimeError("second failure")
            return {"ok": True}

        def is_connection_error(self, error):
            return error is native and case.get("dead", True)

        async def reconnect(self, session):
            record(f"reconnect:{session}")

        def log_recovery(self, resend):
            record("log:resend" if resend else "log:once")

        def targets(self, call):
            return "ns=2;i=5"

        def authorize(self, call):
            record(f"authorize:{call.attempt}")

        def allowed(self, call):
            record(f"allowed:{call.attempt}")

        def denied(self, call, reason):
            record(f"denied:{call.attempt}")

    if "error" in case or "raw" in case:
        with pytest.raises(Exception) as raised:
            await invoke_tool(Port(), call)
        assert (
            str(raised.value) == message(case["error"], **case["fields"])
            if "error" in case
            else str(raised.value) == case["raw"]
        )
        if "error" in case:
            assert raised.value.__cause__ is native
    else:
        assert await invoke_tool(Port(), call) == case["result"]
    assert calls == case["calls"]
    assert call.attempt == case["attempt"]
    assert call.denied == case.get("denied", False)
    assert case == before
