"""Starting one MCP runtime over stdio and calling its tools with a deadline.

The same launch the e2e suite uses (`uv run opcua-mcp-server`, `node
build/index.js`), so a result says something about the code in this checkout
and not about an installed copy. Every call has a deadline: against a real
server "it hung" is a finding, and a harness that waits forever reports nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
NODE_PACKAGE = ROOT / "packages" / "server-node"
NODE_BUILD = NODE_PACKAGE / "build" / "index.js"

#: Seconds allowed for a server to start and finish the MCP handshake. The
#: Python runtime connects to the OPC UA server before it answers `initialize`,
#: so this covers a secure-channel handshake too.
STARTUP_TIMEOUT = 60
CALL_TIMEOUT = 45


def server_params(runtime: str, env: dict[str, str]) -> StdioServerParameters:
    # The environment is built from scratch rather than inherited, apart from
    # what a process needs to start: a stray OPCUA_* variable in the caller's
    # shell must not quietly change what a scenario tests.
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OPCUA_") and key != "NODE_OPTIONS"
    }
    full = {**base, **env}
    if runtime == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=full,
        )
    if runtime == "node":
        return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=full)
    raise ValueError(runtime)


@dataclass
class ToolResult:
    is_error: bool
    text: str
    #: Every text block that parsed as JSON, in order.
    records: list[Any]
    #: structuredContent, where a tool reports `completeness` beside `result`.
    structured: dict[str, Any] | None = None

    def completeness(self) -> dict[str, Any] | None:
        value = (self.structured or {}).get("completeness")
        return value if isinstance(value, dict) else None

    def first(self) -> Any:
        return self.records[0] if self.records else None

    def flat(self) -> list[Any]:
        """Records, with a block that is itself a list spread into its items."""
        out: list[Any] = []
        for record in self.records:
            out.extend(record if isinstance(record, list) else [record])
        return out


def _parse(result) -> ToolResult:
    texts = [b.text for b in result.content if getattr(b, "text", None) is not None]
    records = []
    for text in texts:
        # A plain-text block (an error sentence, a notice) is kept in `text` only.
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            records.append(json.loads(text))
    structured = getattr(result, "structured_content", None)
    return ToolResult(
        bool(result.is_error),
        "\n".join(texts),
        records,
        structured if isinstance(structured, dict) else None,
    )


class Session:
    """An initialised MCP session with deadline-bounded helpers."""

    def __init__(self, session: ClientSession, stderr: Any) -> None:
        self._session = session
        self._stderr = stderr

    async def call(self, tool: str, arguments: dict | None = None, timeout: float = CALL_TIMEOUT):
        with anyio.fail_after(timeout):
            return _parse(await self._session.call_tool(tool, arguments or {}))

    async def tools(self) -> dict[str, dict]:
        with anyio.fail_after(CALL_TIMEOUT):
            listed = await self._session.list_tools()
        return {tool.name: tool.input_schema for tool in listed.tools}

    async def status(self) -> dict:
        result = await self.call("get_server_status")
        record = result.first()
        if not isinstance(record, dict):
            raise RuntimeError(f"get_server_status returned no record: {result.text[:200]}")
        return record

    def stderr(self) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()


@asynccontextmanager
async def open_session(
    runtime: str, env: dict[str, str], log_path: Path | None = None
) -> AsyncIterator[Session]:
    """Start the runtime with `env` and yield a session, its stderr captured.

    A server that refuses to come up raises here; callers that expect a refusal
    (a wrong password, an untrusted certificate) catch it. With `log_path` the
    stderr is appended there too, for whoever triages a failure — a local file
    in the run's work directory, never part of a result.
    """
    with tempfile.TemporaryFile("w+", errors="replace") as errlog:
        try:
            async with _stdio_session(runtime, env, errlog) as session:
                yield session
        finally:
            if log_path is not None:
                errlog.seek(0)
                with log_path.open("a", errors="replace") as out:
                    out.write(f"--- {runtime} session ---\n{errlog.read()}\n")


@asynccontextmanager
async def _stdio_session(runtime: str, env: dict[str, str], errlog) -> AsyncIterator[Session]:
    params = server_params(runtime, env)
    async with (
        stdio_client(params, errlog=errlog) as (read, write),
        ClientSession(read, write) as session,
    ):
        with anyio.fail_after(STARTUP_TIMEOUT):
            await session.initialize()
        yield Session(session, errlog)


def _node_version() -> str:
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip().lstrip("v")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _package_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def describe(runtime: str) -> dict[str, str]:
    """What a result needs to say about the runtime that produced it."""
    if runtime == "python":

        def version(dist: str) -> str:
            try:
                return metadata.version(dist)
            except metadata.PackageNotFoundError:
                return "unknown"

        return {
            "runtime": "python",
            "runtime_version": platform.python_version(),
            "package_version": version("opcua-mcp-server"),
            "opcua_library": f"opcua {version('opcua')}",
            "mcp_sdk": f"mcp {version('mcp')}",
        }
    if runtime == "node":
        package = _package_json(NODE_PACKAGE / "package.json")
        client = _package_json(NODE_PACKAGE / "node_modules" / "node-opcua-client" / "package.json")
        sdk = _package_json(
            NODE_PACKAGE / "node_modules" / "@modelcontextprotocol" / "sdk" / "package.json"
        )
        return {
            "runtime": "node",
            "runtime_version": _node_version(),
            "package_version": package.get("version", "unknown"),
            "opcua_library": f"node-opcua-client {client.get('version', 'unknown')}",
            "mcp_sdk": f"@modelcontextprotocol/sdk {sdk.get('version', 'unknown')}",
        }
    raise ValueError(runtime)


def available(runtime: str) -> str | None:
    """None if the runtime can be started, else why not."""
    if runtime == "node" and not NODE_BUILD.exists():
        return "Node server not built — run `npm ci && npm run build` in packages/server-node"
    return None
