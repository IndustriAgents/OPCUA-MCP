"""Alarm port cases keep refusal/service ordering, tool shapes and native timeout causes."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server import events
from opcua_mcp_server.adapters.opcua_alarms import PythonOpcuaAlarmPort
from opcua_mcp_server.application.alarms import act_on_alarm, list_alarms
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal, message

CASES = json.loads((ROOT / "tests/fixtures/alarms-port.json").read_text(encoding="utf-8"))["cases"]


class FakePort:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.native = TimeoutError("native timeout")

    def record(self, name, **fields):
        self.calls.append({"name": name, **fields})
        if self.case.get("crash") == name:
            raise AdapterFailure(name, "native timeout", self.native) from self.native

    async def list(self, node_id, timeout):
        self.record("list", node_id=node_id, timeout=timeout)
        return self.case["records"]

    def remember(self, records):
        self.record("remember", records=records)

    def condition_for(self, event_id):
        self.record("condition", event_id=event_id)
        return self.case["cached"]

    async def action(self, condition_id, event_id, action, comment, duration):
        self.record(
            "action",
            condition_id=condition_id,
            event_id=event_id,
            action=action,
            comment=comment,
            duration=duration,
        )
        return self.case["status"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_alarm_port(case):
    before = copy.deepcopy(case)
    port = FakePort(case)

    async def invoke():
        if case.get("operation") == "list":
            return await list_alarms(port, case["node_id"], case["timeout"])
        return await act_on_alarm(
            port,
            case["event_id"],
            case["action"],
            case["comment"],
            case["duration"],
            case["condition_id"],
            case["acknowledgement"],
        )

    if "error" in case:
        with pytest.raises((ApplicationRefusal, AdapterFailure)) as raised:
            await invoke()
        assert str(raised.value) == message(case["error"], **case["fields"])
        if case.get("crash"):
            assert raised.value.__cause__.__cause__ is port.native
    else:
        assert await invoke() == case["expected"]
    assert port.calls == case["calls"]
    assert case == before


async def test_native_alarm_timeout_keeps_original_cause_and_one_call(monkeypatch):
    original = TimeoutError("native timeout")
    calls = []

    def call(requests):
        calls.extend(requests)
        raise original

    node = SimpleNamespace(nodeid=ua.NodeId(7, 2), server=SimpleNamespace(call=call))
    client = SimpleNamespace(get_node=lambda _: node)
    monkeypatch.setattr(
        events, "_action_method", lambda *_: SimpleNamespace(nodeid=ua.NodeId(9111))
    )
    port = PythonOpcuaAlarmPort(lambda: client, lambda: None)
    with pytest.raises(AdapterFailure) as raised:
        await port.action("ns=2;i=7", "AQ==", "acknowledge", "", None)
    assert raised.value.__cause__ is original
    assert len(calls) == 1
