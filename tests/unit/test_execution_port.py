"""One native-free authorization/audit path for every control-tool declaration."""

from __future__ import annotations

import copy
import json

import pytest
from conftest import ROOT
from opcua_mcp_server.application.execution import execute_tool
from opcua_mcp_server.errors import ApplicationRefusal

CASES = json.loads((ROOT / "tests/fixtures/execution-port.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_execution_port(case):
    before = copy.deepcopy(case)
    calls = []
    original = RuntimeError("private native details")

    def record(name):
        calls.append(name)
        if case.get("crash") == name:
            raise original if name == "run" else ApplicationRefusal("test refusal")

    class Port:
        def new_call_id(self):
            record("id")
            return "0123456789abcdef"

        async def wait_for_connection(self):
            record("wait")

        def authorize(self, name, args):
            assert name == case["tool"]
            assert args == case["arguments"]
            record("authorize")

        def allowed(self, call):
            assert call.call_id == "0123456789abcdef"
            record(f"allowed:{call.attempt}")
            if case.get("crash") == "allowed":
                raise ApplicationRefusal("test refusal")

        def after(self, call, decision, reason=""):
            record(f"{decision}:{call.attempt}")
            if decision == "failed":
                assert reason == "safe operation failure"

        async def run(self, call):
            if case.get("attempt"):
                call.attempt = case["attempt"]
            if case.get("denied"):
                calls.append("run")
                call.denied = True
                self.after(call, "denied", "policy changed")
                raise original
            record("run")
            return case["result"]

        def normalize_failure(self, name, error):
            record("normalize-failure")
            assert error is original
            return ApplicationRefusal("safe operation failure")

        def normalize_result(self, result):
            record("normalize-result")
            return result

    if "error" in case:
        with pytest.raises(ApplicationRefusal, match=case["error"]):
            await execute_tool(Port(), case["tool"], case["arguments"])
    else:
        assert await execute_tool(Port(), case["tool"], case["arguments"]) == case["result"]
    assert calls == case["calls"]
    assert case == before


async def test_invalid_shape_is_denied_before_wait_authorization_or_operation():
    calls = []

    class Port:
        def new_call_id(self):
            return "id"

        def after(self, call, decision, reason=""):
            calls.append(decision)

        async def wait_for_connection(self):
            raise AssertionError("must not wait")

        def authorize(self, *_):
            raise AssertionError("must not authorize")

        def allowed(self, *_):
            raise AssertionError("must not allow")

        async def run(self, *_):
            raise AssertionError("must not run")

    with pytest.raises(ValueError, match="nodes"):
        await execute_tool(Port(), "write_opcua_nodes", {"nodes": []})
    assert calls == ["denied"]
