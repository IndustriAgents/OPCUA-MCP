"""Status cases pin fast offline responses and post-read capability snapshots."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua_mcp_server.adapters.opcua_diagnostics import PythonOpcuaDiagnosticsPort
from opcua_mcp_server.application.diagnostics import get_server_status
from opcua_mcp_server.errors import AdapterFailure

CASES = json.loads((ROOT / "tests/fixtures/diagnostics-port.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_diagnostics_port(case):
    before = copy.deepcopy(case)
    calls = []

    class Port:
        generation = 1

        def snapshot(self):
            calls.append("snapshot")
            return case["snapshot"]

        async def read(self, security, identity):
            calls.append("read")
            assert security == case["security"]
            assert identity == case["identity"]
            if case["crash"]:
                raise AdapterFailure(
                    "diagnostics", "native timeout", TimeoutError("native timeout")
                )
            self.generation = 2
            return case["status"]

        def capabilities(self):
            calls.append("capabilities")
            return {"session_generation": self.generation}

    assert await get_server_status(Port(), case["security"], case["identity"]) == case["expected"]
    assert calls == case["calls"]
    assert case == before


async def test_native_diagnostics_timeout_retains_cause_before_retry_classification():
    original = TimeoutError("native timeout")
    calls = []
    classified = []

    def get_value():
        calls.append("read")
        raise original

    def run(read):
        try:
            return read()
        except Exception as error:
            classified.append(error)
            raise

    client = SimpleNamespace(get_node=lambda _: SimpleNamespace(get_value=get_value))
    port = PythonOpcuaDiagnosticsPort(
        SimpleNamespace(url="opc.tcp://test:4840", client=client, run=run), lambda: {}
    )
    with pytest.raises(AdapterFailure) as raised:
        await port.read("None", CASES[0]["identity"])
    assert raised.value.__cause__ is original
    assert raised.value is classified[0]
    assert calls == ["read"]
