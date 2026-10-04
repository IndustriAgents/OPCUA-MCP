"""A fake write port characterizes preparation order and the single-send boundary."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua_mcp_server.adapters.opcua_write import PythonOpcuaWritePort
from opcua_mcp_server.application.write import write_nodes
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal, message

CASES = json.loads((ROOT / "tests/fixtures/write-port.json").read_text(encoding="utf-8"))["cases"]


class FakePort:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.native = TimeoutError("native timeout")
        self.prepared = 0

    def record(self, name, **fields):
        self.calls.append({"name": name, **fields})
        if self.case.get("crash") == name:
            raise AdapterFailure(name, "native timeout", self.native) from self.native

    async def current(self, nodes, indices, chunk):
        self.record("current", indices=indices, chunk=chunk)
        return {int(k): v for k, v in self.case.get("current", {}).items()}

    async def engineering(self, node_ids):
        self.record("engineering", node_ids=node_ids)
        return self.case.get("engineering", {})

    async def prepare(self, node, index):
        self.record("prepare", index=index)
        if self.case.get("refuse_at") == index:
            raise ApplicationRefusal(self.case["refusal"])
        failure = self.case.get("failures", {}).get(str(index))
        if not failure:
            self.prepared += 1
        return failure

    async def send(self):
        self.record("send")
        return self.case.get("statuses", ["Good"] * self.prepared)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_write_port(case):
    before = copy.deepcopy(case)
    port = FakePort(case)
    args = (
        port,
        case["nodes"],
        case["limits"],
        {int(k): v for k, v in case["bounds"].items()},
        case["allow_out_of_range"],
    )
    if "error" in case or "refusal" in case:
        with pytest.raises((ApplicationRefusal, AdapterFailure)) as raised:
            await write_nodes(*args)
        expected = (
            case["refusal"]
            if "refusal" in case
            else message(case["error"], **case.get("fields", {}))
        )
        assert str(raised.value) == expected
        if case.get("crash"):
            assert raised.value.__cause__.__cause__ is port.native
    else:
        assert await write_nodes(*args) == case["expected"]
    assert port.calls == case["calls"]
    assert case == before


async def test_native_write_timeout_keeps_original_cause_and_one_attempt():
    original = TimeoutError("native timeout")
    attempts = []

    def send(*args):
        attempts.append(args)
        raise original

    port = PythonOpcuaWritePort(
        SimpleNamespace(uaclient=SimpleNamespace(set_attributes=send)), None
    )
    with pytest.raises(AdapterFailure) as raised:
        await port.send()
    assert raised.value.__cause__ is original
    assert len(attempts) == 1
