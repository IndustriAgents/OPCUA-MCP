"""Fault injection for session identity, cancellation and bounded retained state."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace as NS

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua import ua
from opcua_mcp_server.adapters.opcua_methods import PythonOpcuaMethodPort
from opcua_mcp_server.application.execution import ExecutionCall
from opcua_mcp_server.connection import OpcuaConnection
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.infrastructure.control_context import ControlContext, run_control
from opcua_mcp_server.node_metadata import NodeMetadata
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config
from opcua_mcp_server.resources import Admission, BoundedCache, BoundedRecords
from opcua_mcp_server.subscriptions import SubscriptionManager

FIXTURE = json.loads((ROOT / "tests/fixtures/architecture-safety.json").read_text(encoding="utf-8"))


def policy():
    return ToolPolicy(
        parse_policy_config(
            {
                "OPCUA_PROFILE": "operator",
                "OPCUA_SECURITY_POLICY": "Basic256Sha256",
                "OPCUA_SERVER_CERT": "/pki/server.pem",
                "OPCUA_ALLOWED_WRITE_NODES": FIXTURE["namespaces"]["allowed"],
                "OPCUA_ALLOWED_METHODS": "ns=2;i=1|ns=2;i=2",
            }
        )
    )


def test_failed_namespace_refresh_revokes_old_uri_permission():
    subject = policy()
    subject.bind_namespaces(FIXTURE["namespaces"]["initial"])
    args = {"nodes": [{"node_id": FIXTURE["namespaces"]["target"], "value": 1}]}
    subject.authorize("write_opcua_nodes", args)
    conn = OpcuaConnection("opc.tcp://unused", policy=subject)
    client = NS(get_node=lambda _: NS(get_value=lambda: (_ for _ in ()).throw(TimeoutError())))
    conn._bind_policy_namespaces(client)
    with pytest.raises(PermissionError):
        subject.authorize("write_opcua_nodes", args)


@pytest.mark.parametrize("case", FIXTURE["buffers"], ids=lambda c: c["name"])
def test_buffer_count_and_byte_bounds(case):
    records = BoundedRecords(case["capacity"], case["bytes"])
    for record in case["records"]:
        records.append(record)
    assert list(records) == case["retained"]
    assert records.byte_count <= case["bytes"]
    while records:
        records.popleft()
    assert records.byte_count == 0


def test_cache_evicts_oldest_entries():
    case = FIXTURE["cache"]
    cache = BoundedCache(case["capacity"])
    cache.update(dict(case["entries"]))
    assert list(cache.items()) == [tuple(entry) for entry in case["retained"]]


def test_pending_subscriptions_consume_capacity(monkeypatch):
    manager = SubscriptionManager()
    manager.attach(object())
    monkeypatch.setattr(manager, "_attach", lambda *_: None)
    for i in range(CONTRACT["limits"]["maxSubscriptions"] - 1):
        manager.subscribe(f"ns=2;i={i}")
    started, finish = threading.Event(), threading.Event()

    def attach(*_):
        started.set()
        assert finish.wait(3)

    monkeypatch.setattr(manager, "_attach", attach)
    worker = threading.Thread(target=lambda: manager.subscribe("ns=2;i=999"))
    worker.start()
    assert started.wait(3)
    try:
        with pytest.raises(ValueError, match="subscription"):
            manager.subscribe("ns=2;i=1000")
    finally:
        finish.set()
        worker.join(3)
    assert len(manager.list()) == CONTRACT["limits"]["maxSubscriptions"]


def test_control_ranges_do_not_reuse_best_effort_failures(monkeypatch):
    metadata = NodeMetadata()
    monkeypatch.setattr(
        metadata, "_read", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("unreadable"))
    )
    assert metadata.for_nodes(None, ["ns=2;i=5"]) == {"ns=2;i=5": None}
    with pytest.raises(ValueError, match="Nothing was written"):
        metadata.for_control(None, ["ns=2;i=5"])


def test_overload_keeps_diagnostics_available():
    admission = Admission()
    admission.active = CONTRACT["limits"]["maxConcurrentCalls"]
    with pytest.raises(ValueError, match="Resource limit"), admission.enter("read_opcua_nodes"):
        pytest.fail("overload admitted")
    with admission.enter("get_server_status"):
        assert admission.active == CONTRACT["limits"]["maxConcurrentCalls"]


async def test_cancelled_native_method_finishes_accounting_without_resend():
    sample = FIXTURE["control"]
    started, finish = threading.Event(), threading.Event()
    sends = []

    def native(requests):
        sends.append(requests)
        started.set()
        assert finish.wait(3)
        return [NS(StatusCode=ua.StatusCode(), OutputArguments=[])]

    client = NS(
        get_node=lambda text: NS(nodeid=ua.NodeId.from_string(text), server=NS(call=native))
    )
    call = ExecutionCall(
        sample["tool"],
        {"object_node_id": sample["object"], "method_node_id": sample["method"]},
        next(t for t in CONTRACT["tools"] if t["name"] == sample["tool"]),
        "id",
        session=sample["session"],
    )
    connection = NS(session_id=sample["session"], control_call=lambda operation: operation())
    context = ControlContext(call, connection, policy())
    port = PythonOpcuaMethodPort(client)
    tasks = set()
    task = asyncio.create_task(
        run_control(context, lambda: port.call(sample["object"], sample["method"], []), tasks)
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
    finally:
        finish.set()
    result = await task
    assert result["status"] == "Good"
    assert len(sends) == 1
    assert not tasks


async def test_session_change_during_preflight_prevents_native_send():
    sample = FIXTURE["control"]
    call = ExecutionCall(
        sample["tool"],
        {"object_node_id": sample["object"], "method_node_id": sample["method"]},
        next(t for t in CONTRACT["tools"] if t["name"] == sample["tool"]),
        "id",
        session=sample["session"],
    )
    connection = NS(session_id="replacement", control_call=lambda operation: operation())
    context = ControlContext(call, connection, policy())
    sends = []
    with pytest.raises(ValueError, match="session changed"):
        await run_control(
            context, lambda: asyncio.to_thread(context.dispatch, lambda: sends.append(1)), set()
        )
    assert not sends


def test_cache_byte_budget_is_released_on_clear():
    cache = BoundedCache(10, 25)
    cache["a"] = "1234567890"
    cache["b"] = "1234567890"
    assert list(cache) == ["b"]
    assert cache.byte_count <= 25
    cache.clear()
    assert cache.byte_count == 0


async def test_cancellation_before_send_prevents_late_dispatch():
    sample = FIXTURE["control"]
    call = ExecutionCall(sample["tool"], {}, {}, "id", session=sample["session"])
    connection = NS(session_id=sample["session"], control_call=lambda operation: operation())
    context = ControlContext(call, connection, policy())
    started = asyncio.Event()
    sends = []

    async def prepare():
        started.set()
        await asyncio.Future()
        return context.dispatch(lambda: sends.append(1))

    tasks = set()
    operation = asyncio.create_task(run_control(context, prepare, tasks))
    await started.wait()
    operation.cancel()
    with pytest.raises(ValueError, match="Nothing was sent"):
        await operation
    assert not sends
    await asyncio.sleep(0)
    assert not tasks


async def test_cancelled_sent_control_has_bounded_uncertain_accounting(monkeypatch):
    sample = FIXTURE["control"]
    spec = next(t for t in CONTRACT["tools"] if t["name"] == sample["tool"])
    call = ExecutionCall(
        sample["tool"],
        {
            "object_node_id": sample["object"],
            "method_node_id": sample["method"],
            "arguments": [9999],
        },
        spec,
        "id",
        session=sample["session"],
    )
    connection = NS(session_id=sample["session"], control_call=lambda operation: operation())
    context = ControlContext(call, connection, policy())
    started, finish = asyncio.Event(), asyncio.Event()

    async def native():
        context.dispatch(lambda: None)
        started.set()
        await finish.wait()

    monkeypatch.setitem(CONTRACT["limits"], "controlAccountingTimeoutSeconds", 0.01)
    tasks = set()
    operation = asyncio.create_task(run_control(context, native, tasks))
    await started.wait()
    operation.cancel()
    try:
        with pytest.raises(ValueError, match="whether it took effect is unknown") as caught:
            await operation
        assert "9999" not in str(caught.value)
    finally:
        finish.set()
        await asyncio.gather(*tasks)
    assert not tasks


def test_late_lookup_cannot_refill_cache_after_session_change():
    from opcua_mcp_server.resources import cache_generation

    owner = NS(cache=BoundedCache())

    @cache_generation
    def old_lookup(subject):
        subject.cache.clear()
        subject.cache["stale"] = "old session"

    old_lookup(owner)
    assert not owner.cache
    owner.cache["fresh"] = "new session"
    assert list(owner.cache) == ["fresh"]


@pytest.mark.parametrize("failure", [False, True])
async def test_cancelled_control_records_a_terminal_outcome(failure):
    from opcua_mcp_server.application.execution import execute_tool
    from opcua_mcp_server.application.invocation import invoke_tool

    sample = FIXTURE["control"]
    started, finish = asyncio.Event(), asyncio.Event()
    decisions = []
    tasks = set()
    connection = NS(session_id=sample["session"], control_call=lambda operation: operation())
    subject = policy()

    class Port:
        def new_call_id(self):
            return "id"

        async def wait_for_connection(self):
            pass

        def authorize(self, *args):
            pass

        def after(self, call, decision, reason=""):
            decisions.append(decision)

        def normalize_failure(self, name, error):
            return error

        def normalize_result(self, result):
            return result

        async def run(self, call):
            return await invoke_tool(self, call)

        def has_connection(self):
            return True

        async def connect(self):
            pass

        async def capabilities(self, call):
            pass

        def session(self):
            return sample["session"]

        def allowed(self, call):
            decisions.append("allowed")

        def denied(self, call, reason):
            decisions.append("denied")

        async def dispatch(self, call):
            context = ControlContext(call, connection, subject)

            async def native():
                context.dispatch(lambda: None)
                started.set()
                await finish.wait()
                if failure:
                    raise TimeoutError("reply lost after dispatch")
                return "applied"

            return await run_control(context, native, tasks)

    arguments = {"object_node_id": sample["object"], "method_node_id": sample["method"]}
    operation = asyncio.create_task(execute_tool(Port(), sample["tool"], arguments))
    await started.wait()
    operation.cancel()
    finish.set()
    if failure:
        with pytest.raises(ValueError, match="whether it took effect is unknown"):
            await operation
        assert decisions == ["allowed", "uncertain"]
    else:
        assert await operation == "applied"
        assert decisions == ["allowed", "completed"]


async def test_uncertain_audit_retains_sending_session_and_uri(monkeypatch):
    from mcp.server.mcpserver import MCPServer
    from opcua_mcp_server.audit import AuditSink
    from opcua_mcp_server.server import create_server
    from opcua_mcp_server.state import ServerState

    subject = policy()
    subject.bind_namespaces(FIXTURE["namespaces"]["initial"])
    sink = AuditSink()
    records = []
    monkeypatch.setattr(sink, "write", records.append)
    state = ServerState(policy=subject, audit=sink)
    connection = NS(
        session_id="original",
        session_generation=1,
        connecting=False,
        ensure_connected=lambda: None,
        url=state.url,
    )

    def reconnect(_):
        connection.session_id = "replacement"
        connection.session_generation = 2
        subject.bind_namespaces(FIXTURE["namespaces"]["reordered"])
        raise ConnectionRefusedError("endpoint still offline")

    connection.reconnect = reconnect
    state.connection = connection

    async def lost_reply(*_):
        raise TimeoutError("reply lost")

    monkeypatch.setattr(MCPServer, "call_tool", lost_reply)
    app = create_server(state)
    with pytest.raises(ToolError, match="whether it took effect is unknown"):
        await app.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": FIXTURE["namespaces"]["target"], "value": 1}]},
        )
    assert [record["decision"] for record in records] == ["allowed", "uncertain"]
    assert [record["session"] for record in records] == ["original", "original"]
    assert [record["session_generation"] for record in records] == [1, 1]
    assert records[1]["node_uris"] == [FIXTURE["namespaces"]["allowed"]]
