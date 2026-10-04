"""Event application ports preserve applied settings, loss notices and fetched counts."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua_mcp_server.adapters.opcua_events import PythonOpcuaEventPort
from opcua_mcp_server.application.events import read_event_history, read_events, subscribe_events
from opcua_mcp_server.datetimes import format_iso_utc, parse_iso_datetime
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal, message

FIXTURE = json.loads((ROOT / "tests/fixtures/events-port.json").read_text(encoding="utf-8"))


class FakePort:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.native = TimeoutError("native timeout")

    def record(self, name, **fields):
        self.calls.append({"name": name, **fields})
        if self.case.get("crash") == name:
            raise AdapterFailure(name, "native timeout", self.native) from self.native

    async def subscribe(self, node_id, severity, size):
        self.record("subscribe", node_id=node_id, severity=severity, size=size)
        return self.case["replaced"]

    async def drain(self, node_id, limit):
        self.record("drain", node_id=node_id, limit=limit)
        return self.case["drain"]

    async def history(self, node_id, start, end, wanted, severity):
        self.record(
            "history",
            node_id=node_id,
            start=format_iso_utc(start),
            end=format_iso_utc(end),
            wanted=wanted,
            severity=severity,
        )
        page = self.case["page"]
        return {**page, "last_time": page["lastTime"]}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
async def test_event_port(case):
    before = copy.deepcopy(case)
    port = FakePort(case)

    async def invoke():
        if case["operation"] == "subscribe":
            return await subscribe_events(port, case["nodeId"], case["severity"], case["requested"])
        if case["operation"] == "drain":
            return await read_events(port, case["nodeId"], case["limit"])
        q = case["request"]
        return await read_event_history(
            port,
            {
                "node_id": q["nodeId"],
                "num_values": q["numValues"],
                "severity_min": q["severityMin"],
            },
            now=lambda: parse_iso_datetime(FIXTURE["now"]),
        )

    if "error" in case:
        with pytest.raises((AdapterFailure, ApplicationRefusal)) as raised:
            await invoke()
        assert str(raised.value) == message(case["error"], **case["fields"])
        if case.get("crash"):
            assert raised.value.__cause__.__cause__ is port.native
    else:
        assert await invoke() == case["expected"]
    assert port.calls == case["calls"]
    assert case == before


async def test_native_event_subscribe_timeout_keeps_original_cause_and_one_attempt():
    original = TimeoutError("native timeout")
    calls = []

    def subscribe(*args):
        calls.append(args)
        raise original

    port = PythonOpcuaEventPort(None, SimpleNamespace(subscribe=subscribe))
    with pytest.raises(AdapterFailure) as raised:
        await port.subscribe("i=85", 0, 10)
    assert raised.value.__cause__ is original
    assert len(calls) == 1
