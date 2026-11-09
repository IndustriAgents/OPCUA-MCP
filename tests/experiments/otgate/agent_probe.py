"""Run with the agent credential inside an outbound network sandbox."""

import asyncio
import errno
import os
import socket
import sys
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def probe(url, blocked_ports):
    headers = {"Authorization": f"Bearer {os.environ['OTGATE_TEST_AGENT_TOKEN']}"}
    async with (
        httpx.AsyncClient(headers=headers, trust_env=False, timeout=5) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session,
    ):
        await session.initialize()
        assert {tool.name for tool in (await session.list_tools()).tools} == {
            "browse",
            "read_tag",
            "write_tag",
        }
        result = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=111", "value": 4.125}]}
        )
        assert result.isError, "bridge write tool is exposed to the agent"

    for port in blocked_ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                raise AssertionError(f"Agent could connect to forbidden port {port}")
        except PermissionError as error:
            assert error.errno in (errno.EPERM, errno.EACCES), error
    print("PASS: sandboxed agent reaches only its MCP listener; direct OPC UA and approval blocked")


if __name__ == "__main__":
    asyncio.run(probe(sys.argv[1], [int(port) for port in sys.argv[2:]]))
