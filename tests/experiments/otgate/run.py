"""Exercise otgate over HTTP, the bridge over stdio, and the bundled mock over OPC UA."""

import argparse
import asyncio
import json
import logging
import secrets
import socket
import sys
from contextlib import asynccontextmanager, closing, nullcontext
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn
from anyio import BrokenResourceError, ClosedResourceError
from asyncua import Client
from mcp import ClientSession, McpError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from otgate.audit import AuditLog
from otgate.backends.base import Backend, BackendError, BackendPolicyDenied
from otgate.gateway import Gateway
from otgate.models import Access, Interlock, Policy, Rule
from otgate.server import build_agent_server, build_http_app, build_operator_server

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parents[2] / "contract" / "tools.json"
LOOPBACK = "127.0.0.1"
PERMIT = "ns=2;i=110"
SETPOINT = "ns=2;i=111"
EXCLUDED = "ns=2;i=113"
READ_TIMEOUT = timedelta(seconds=10)


def text_result(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


class BridgeBackend(Backend):
    """Adapter for the two scalar operations otgate needs in this experiment."""

    def __init__(self, session: ClientSession):
        self.session = session
        with CONTRACT.open(encoding="utf-8") as stream:
            self.allowlist_denial = json.load(stream)["errors"]["nodeNotWritable"]

    async def connect(self) -> None:
        # The harness owns the initialized session.
        pass

    async def disconnect(self) -> None:
        pass

    async def call_one(self, tool, arguments, *, policy_denial=None):
        try:
            result = await self.session.call_tool(tool, arguments)
        except (McpError, BrokenResourceError, ClosedResourceError, OSError) as exc:
            raise BackendError(f"{tool} request failed: {exc}") from exc
        if result.isError:
            detail = text_result(result)
            # The bridge returns this pre-dispatch denial as MCP error text.
            # Match the full contract message for this write's exact node.
            if policy_denial and len(result.content) == 1 and detail == policy_denial:
                raise BackendPolicyDenied(detail)
            raise BackendError(detail)
        rows = (result.structuredContent or {}).get("result")
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("status") != "Good":
            raise BackendError(f"{tool} returned a failed or malformed per-node result: {rows}")
        return rows[0]

    async def read(self, node_id):
        row = await self.call_one("read_opcua_nodes", {"node_ids": [node_id]})
        return row["value"]

    async def write(self, node_id, value):
        await self.call_one(
            "write_opcua_nodes",
            {"nodes": [{"node_id": node_id, "value": value}]},
            policy_denial=self.allowlist_denial.format(node_id=node_id),
        )


@asynccontextmanager
async def simulator(workspace_python, out):
    with closing(socket.create_server((LOOPBACK, 0))) as listener:
        port = listener.getsockname()[1]
    endpoint = f"opc.tcp://{LOOPBACK}:{port}/freeopcua/server/"
    with (out / "simulator.log").open("w", encoding="utf-8") as log:
        proc = await asyncio.create_subprocess_exec(
            workspace_python,
            str(HERE / "recording_mock.py"),
            str(out / "wire.jsonl"),
            "--endpoint",
            endpoint,
            "--no-history",
            stdout=log,
            stderr=log,
        )
        try:
            async with asyncio.timeout(15):
                while True:
                    if proc.returncode is not None:
                        raise RuntimeError(f"Bundled simulator exited. See {out / 'simulator.log'}")
                    try:
                        _, writer = await asyncio.open_connection(LOOPBACK, port)
                    except ConnectionRefusedError:
                        await asyncio.sleep(0.05)
                    else:
                        writer.close()
                        await writer.wait_closed()
                        break
            yield endpoint, port
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()


@asynccontextmanager
async def http_channel(app):
    with closing(socket.create_server((LOOPBACK, 0))) as listener:
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        # The parent process owns signals, not either test listener.
        server.capture_signals = nullcontext
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("otgate HTTP listener exited during startup")
                    await asyncio.sleep(0.02)
            yield f"http://{LOOPBACK}:{port}/mcp", port
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@asynccontextmanager
async def http_session(url, token):
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write, read_timeout_seconds=READ_TIMEOUT) as session,
    ):
        await session.initialize()
        yield session


def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def call_text(session, name, **args):
    result = await session.call_tool(name, args)
    assert not result.isError, text_result(result)
    return text_result(result)


async def request_write(agent, operator, wire, node, value):
    before = rows(wire)
    assert (await call_text(agent, "write_tag", node_id=node, value=value)).startswith("ASK:")
    result = await operator.call_tool("list_pending")
    assert not result.isError, text_result(result)
    [pending] = result.structuredContent["result"]
    assert (pending["node_id"], pending["value"], pending["status"]) == (node, value, "PENDING")
    assert rows(wire) == before
    return pending["id"]


async def exercise(agent, operator, hmi, wire):
    await hmi.get_node(PERMIT).write_value(True)
    # The only wire activity so far is the explicit fixture write.
    assert rows(wire) == [{"node": PERMIT, "value": True}]

    # A distinct fractional value makes accidental type coercion visible.
    setpoint = 4.125
    result = await call_text(agent, "write_tag", node_id=SETPOINT, value=31.25)
    assert result.startswith("DENY:") and "outside allowed range" in result, result
    assert rows(wire) == [{"node": PERMIT, "value": True}]

    pending = await request_write(agent, operator, wire, SETPOINT, setpoint)
    before = rows(wire)
    self_approval = await agent.call_tool("approve", {"request_id": pending})
    assert self_approval.isError, text_result(self_approval)
    assert rows(wire) == before
    assert (await call_text(operator, "approve", request_id=pending)).startswith(
        "APPROVED (executed):"
    )
    assert rows(wire) == [*before, {"node": SETPOINT, "value": setpoint}]
    assert float(await call_text(agent, "read_tag", node_id=SETPOINT)) == setpoint
    assert await hmi.get_node(SETPOINT).read_value() == setpoint
    assert (await call_text(operator, "approve", request_id=pending)).startswith(
        "APPROVED (not executed):"
    )
    assert rows(wire) == [*before, {"node": SETPOINT, "value": setpoint}]
    print("PASS: approval binds target/value; pending and repeated approval send zero writes")

    pending = await request_write(agent, operator, wire, SETPOINT, setpoint)
    await hmi.get_node(PERMIT).write_value(False)
    before = rows(wire)
    result = await call_text(operator, "approve", request_id=pending)
    assert result.startswith("DENIED (not executed):") and "interlock active" in result, result
    assert rows(wire) == before

    await hmi.get_node(PERMIT).write_value(True)
    pending = await request_write(agent, operator, wire, SETPOINT, setpoint)
    await hmi.get_node(SETPOINT).write_value(25.0)
    before = rows(wire)
    result = await call_text(operator, "approve", request_id=pending)
    assert result.startswith("DENIED (not executed):") and "rate of change" in result, result
    assert rows(wire) == before
    assert await hmi.get_node(SETPOINT).read_value() == 25.0
    print("PASS: live guard and rate checks each reject stale approval before a wire write")

    pending = await request_write(agent, operator, wire, EXCLUDED, 7.375)
    before = rows(wire)
    result = await call_text(operator, "approve", request_id=pending)
    assert result.startswith("DENIED (not executed):"), result
    assert "not writable under the operator policy" in result, result
    assert rows(wire) == before
    assert await hmi.get_node(EXCLUDED).read_value() == 10.0
    remaining = await operator.call_tool("list_pending")
    assert not remaining.isError, text_result(remaining)
    assert remaining.structuredContent["result"] == []
    repeated = await call_text(operator, "approve", request_id=pending)
    assert repeated.startswith("DENIED (not executed): request already resolved:"), repeated
    assert rows(wire) == before
    print("PASS: bridge allowlist blocks dispatch and resolves the approval as denied")


async def boundary(agent_url, agent_port, operator_url, operator_port, simulator_port, token):
    # A bearer-token test alone would not prove that a local process can't
    # bypass MCP by connecting directly to the simulated plant.
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        response = await client.post(
            operator_url, headers={"Authorization": f"Bearer {token}"}, json={}
        )
        assert response.status_code == 401, response.status_code

    profile = (
        "(version 1) (allow default) (deny network-outbound) "
        f'(allow network-outbound (remote ip "localhost:{agent_port}"))'
    )
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        profile,
        sys.executable,
        str(HERE / "agent_probe.py"),
        agent_url,
        str(operator_port),
        str(simulator_port),
        env={"OTGATE_TEST_AGENT_TOKEN": token},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 0, stderr.decode()
    print(stdout.decode().strip())


def verify_audits(out):
    bridge = rows(out / "bridge-audit.jsonl")
    assert [(row["decision"], row["node_ids"]) for row in bridge] == [
        ("allowed", [SETPOINT]),
        ("completed", [SETPOINT]),
        ("denied", [EXCLUDED]),
    ], bridge
    assert all(
        row["tool"] == "write_opcua_nodes" and row["profile"] == "operator" for row in bridge
    )
    assert all("value" not in row for row in bridge)
    raw_bridge = (out / "bridge-audit.jsonl").read_text(encoding="utf-8")
    assert all(value not in raw_bridge for value in ("4.125", "7.375", "31.25"))

    gateway = [row for row in rows(out / "gateway-audit.jsonl") if row["action"] == "write"]
    assert [(r["node_id"], r["value"], r["decision"], r["executed"]) for r in gateway] == [
        (SETPOINT, 31.25, "DENY", False),
        (SETPOINT, 4.125, "ASK", False),
        (SETPOINT, 4.125, "ALLOW", True),
        (SETPOINT, 4.125, "ASK", False),
        (SETPOINT, 4.125, "DENY", False),
        (SETPOINT, 4.125, "ASK", False),
        (SETPOINT, 4.125, "DENY", False),
        (EXCLUDED, 7.375, "ASK", False),
        (EXCLUDED, 7.375, "DENY", False),
    ], gateway
    print(
        "PASS: separate gateway and bridge audit trails; synthetic values stay out of bridge audit"
    )


async def run(workspace_python, out):
    agent_token, operator_token = secrets.token_urlsafe(), secrets.token_urlsafe()
    policy = Policy(
        {
            SETPOINT: Rule(
                SETPOINT,
                Access.WRITE_WITH_APPROVAL,
                (0, 30),
                max_rate=5,
                rate_interval=60,
                interlocks=(Interlock(PERMIT, "==", False),),
            ),
            EXCLUDED: Rule(EXCLUDED, Access.WRITE_WITH_APPROVAL, (0, 30)),
        }
    )
    async with simulator(workspace_python, out) as (endpoint, simulator_port):
        params = StdioServerParameters(
            command=workspace_python,
            args=["-m", "opcua_mcp_server"],
            # The worker only gets test OPC UA settings. The bundled mock
            # listens on an ephemeral loopback port and has no TLS certificate.
            env={
                "OPCUA_SERVER_URL": endpoint,
                "OPCUA_PROFILE": "operator",
                "OPCUA_ALLOWED_WRITE_NODES": SETPOINT,
                "OPCUA_ALLOW_INSECURE_CONTROL": "true",
                "OPCUA_AUDIT_FILE": str(out / "bridge-audit.jsonl"),
            },
        )
        with (out / "bridge.log").open("w", encoding="utf-8") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write, read_timeout_seconds=READ_TIMEOUT) as bridge,
            ):
                await bridge.initialize()
                gateway = Gateway(
                    policy, BridgeBackend(bridge), AuditLog(out / "gateway-audit.jsonl")
                )
                try:
                    agent_app = build_http_app(build_agent_server(gateway), agent_token, "agent")
                    operator_app = build_http_app(
                        build_operator_server(gateway), operator_token, "operator"
                    )
                    async with (
                        http_channel(agent_app) as (agent_url, agent_port),
                        http_channel(operator_app) as (operator_url, operator_port),
                        http_session(agent_url, agent_token) as agent,
                        http_session(operator_url, operator_token) as operator,
                        Client(endpoint, timeout=5) as hmi,
                    ):
                        await exercise(agent, operator, hmi, out / "wire.jsonl")
                        await boundary(
                            agent_url,
                            agent_port,
                            operator_url,
                            operator_port,
                            simulator_port,
                            agent_token,
                        )
                finally:
                    gateway.close()
    verify_audits(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-python", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if sys.flags.optimize:
        parser.error("Run without Python optimization so assertions stay enabled.")
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        parser.error("This experiment requires macOS sandbox-exec for the network boundary check.")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(filename=out / "gateway.log", level=logging.INFO)
    print(f"Evidence: {out}", flush=True)
    asyncio.run(run(str(args.workspace_python.absolute()), out))


if __name__ == "__main__":
    main()
