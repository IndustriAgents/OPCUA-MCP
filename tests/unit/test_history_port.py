"""History application queries use native-free records and injectable time."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua_mcp_server.adapters.opcua_history import PythonOpcuaHistoryPort
from opcua_mcp_server.application.history import read_history
from opcua_mcp_server.datetimes import format_iso_utc, parse_iso_datetime
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal, message

FIXTURE = json.loads((ROOT / "tests/fixtures/history-port.json").read_text(encoding="utf-8"))


class FakePort:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.native = TimeoutError("native timeout")

    def record(self, name, **fields):
        self.calls.append({"name": name, **fields})
        if self.case.get("crash") == name:
            raise AdapterFailure(name, "native timeout", self.native) from self.native
        if self.case.get("refuse") == name:
            raise ApplicationRefusal(self.case["literal"])

    async def raw(self, node_id, start, end, wanted):
        self.record(
            "raw",
            node_id=node_id,
            start=format_iso_utc(start),
            end=format_iso_utc(end),
            wanted=wanted,
        )
        return {"records": self.case["records"], "continued": self.case.get("continued", False)}

    async def aggregate(self, node_id, start, end, name, interval):
        self.record(
            "aggregate",
            node_id=node_id,
            start=format_iso_utc(start),
            end=format_iso_utc(end),
            function=name,
            interval=interval,
        )
        return self.case["records"]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
async def test_history_port(case):
    before = copy.deepcopy(case)
    q = case["request"]
    port = FakePort(case)
    request = {
        "node_id": q["nodeId"],
        "start": q.get("start"),
        "end": q.get("end"),
        "num_values": q["numValues"],
        "aggregate_function": q.get("aggregateFunction"),
        "processing_interval": q["processingInterval"],
    }

    async def invoke():
        return await read_history(
            port, request, case.get("offered", []), now=lambda: parse_iso_datetime(FIXTURE["now"])
        )

    if "error" in case or "literal" in case:
        with pytest.raises((AdapterFailure, ApplicationRefusal)) as raised:
            await invoke()
        expected = case.get("literal") or message(case["error"], **case.get("fields", {}))
        assert str(raised.value) == expected
        if case.get("crash"):
            assert raised.value.__cause__.__cause__ is port.native
    else:
        assert await invoke() == case["expected"]
    assert port.calls == case["calls"]
    assert case == before


async def test_native_history_timeout_retains_original_cause_before_thread_hop():
    native = TimeoutError("native timeout")
    calls = []

    def read(details):
        calls.append(details)
        raise native

    port = PythonOpcuaHistoryPort(
        SimpleNamespace(get_node=lambda _: SimpleNamespace(history_read=read)), {}
    )
    with pytest.raises(AdapterFailure) as raised:
        await port.raw("ns=2;i=20", None, None, 10)
    assert raised.value.__cause__ is native
    assert len(calls) == 1
