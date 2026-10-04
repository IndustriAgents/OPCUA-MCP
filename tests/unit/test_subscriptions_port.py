"""Shared subscription orchestration cases and native boundary checks."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua_mcp_server.adapters.opcua_subscriptions import PythonOpcuaSubscriptionPort
from opcua_mcp_server.application.subscriptions import (
    Filter,
    list_subscriptions,
    subscribe_nodes,
    unsubscribe_nodes,
)
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal, message

CASES = json.loads((ROOT / "tests/fixtures/subscriptions-port.json").read_text(encoding="utf-8"))[
    "cases"
]


class FakePort:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.index = 0
        self.native = TimeoutError("native timeout")

    def record(self, name, **fields):
        self.calls.append({"name": name, **fields})
        if self.case.get("crash") == name:
            raise AdapterFailure(name, "native timeout", self.native) from self.native

    def list(self):
        self.record("list")
        return self.case["active"]

    async def ranges(self, ids):
        self.record("ranges", node_ids=ids)
        return self.case["ranges"]

    async def subscribe(self, node_id, options, data_filter):
        self.record("subscribe", node_id=node_id)
        assert options == self.case["options"]
        assert data_filter == Filter(
            self.case["filter"]["deadbandType"],
            self.case["filter"]["deadbandValue"],
            self.case["filter"]["trigger"],
        )
        result = self.case["results"][self.index]
        self.index += 1
        return result

    async def unsubscribe(self, subscription_id):
        self.record("unsubscribe", id=subscription_id)
        return next(r for r in self.case["active"] if r["subscription_id"] == subscription_id)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_subscription_port(case):
    before = copy.deepcopy(case)
    port = FakePort(case)

    async def invoke():
        if case["operation"] == "list":
            return list_subscriptions(port)
        if case["operation"] == "unsubscribe":
            return await unsubscribe_nodes(port, case["ids"])
        f = case["filter"]
        return await subscribe_nodes(
            port,
            case["ids"],
            case["options"],
            Filter(f["deadbandType"], f["deadbandValue"], f["trigger"]),
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


async def test_native_subscription_timeout_keeps_original_cause_and_one_attempt():
    native = TimeoutError("native timeout")
    calls = []

    def subscribe(*args):
        calls.append(args)
        raise native

    port = PythonOpcuaSubscriptionPort(lambda: None, SimpleNamespace(subscribe=subscribe), None)
    with pytest.raises(AdapterFailure) as raised:
        await port.subscribe("ns=2;i=1", {}, Filter())
    assert raised.value.__cause__ is native
    assert len(calls) == 1


async def test_offline_list_and_cancellation_never_select_client():
    def unavailable():
        raise AssertionError("client must stay lazy")

    port = PythonOpcuaSubscriptionPort(
        unavailable,
        SimpleNamespace(
            list=lambda: [], unsubscribe=lambda _: {"subscription_id": "sub-1", "dropped": 0}
        ),
        None,
    )
    assert port.list() == []
    assert (await port.unsubscribe("sub-1"))["subscription_id"] == "sub-1"
