"""Shared pytest fixtures for the OPC UA MCP end-to-end suite.

The suite drives the *actual* MCP servers (Python and Node) over stdio using the
official `mcp` client SDK, pointed at the mock industrial OPC UA server. A
session-scoped fixture starts that mock on an ephemeral port and yields its
endpoint, which the tests hand to the MCP servers under test.

Every mock gets a fresh port per session (#46). Fixed ports plus a "reuse
whatever is already listening" branch meant two checkouts running the suite at
once talked to each other's mock as it started, warmed up and was torn down,
producing failures that moved between tests run to run. Owning the server also
means the warmup sleeps below always apply to the history the tests then read.

A second, aggregate-capable mock (`packages/mock-server-aggregate`) backs the
aggregate tests. It is kept separate from the main mock on purpose: the main mock
must keep advertising *no* aggregate functions so the suite can assert that both
MCP servers hide `read_aggregate_opcua_node` when it is unsupported.

A third, *secured* mock (`fixtures/secure_opcua_server.py`) backs the
connection-security tests. It is separate for the same reason: it offers no
unsecured endpoint at all, which is what makes those assertions mean something.

To point the suite at a server you manage yourself — one left running while
iterating, or a real device — set `OPCUA_SERVER_URL` or
`OPCUA_AGGREGATE_SERVER_URL`. The fixture then starts nothing, and the warmup is
yours to arrange.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fixtures.pki import CLIENT_URI, SERVER_URI, write_self_signed

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
SERVER_PATH = "/freeopcua/server/"

# Set to talk to a server you manage yourself instead of one started per session.
SERVER_URL_OVERRIDE = os.environ.get("OPCUA_SERVER_URL")

# Seconds of runtime to allow the mock server to accumulate history before tests
# read it back. The mock writes one history record per variable per second.
HISTORY_WARMUP_SECONDS = 6

# --- aggregate-capable mock (packages/mock-server-aggregate) --------------------
AGGREGATE_PATH = "/UA/Aggregate"
AGGREGATE_SERVER_URL_OVERRIDE = os.environ.get("OPCUA_AGGREGATE_SERVER_URL")
AGGREGATE_MOCK_DIR = ROOT / "packages" / "mock-server-aggregate"

# The aggregate mock ramps its Temperature node by a fixed amount every tick, so
# consecutive Average buckets differ by exactly this much per second of interval.
AGGREGATE_RAMP_PER_SECOND = 1.0
# Node ID of that ramping variable, as reported on the server's READY line.
AGGREGATE_NODE_ID = "ns=1;i=1001"

# The aggregate tests read back windows of up to ~30s, so more warmup is needed
# here than for the raw-history test against the main mock.
AGGREGATE_WARMUP_SECONDS = 20

# The aggregate mock pulls `node-opcua-aggregates`, whose transitive deps
# (@peculiar/x509, @ster5/global-mutex) require Node 20 — npm only warns at
# install time and the server then dies at startup. This is a limitation of the
# test fixture, not of the shipped Node server, whose own dependency tree
# installs cleanly on Node 18.
AGGREGATE_MOCK_MIN_NODE = 20

# --- secured mock (tests/fixtures/secure_opcua_server.py) -----------------------
# A third mock, on its own port, offering *only* Basic256Sha256 endpoints and
# requiring a username. The other two must stay unsecured — the rest of the suite
# depends on connecting to them with no security at all.
SECURE_PATH = "/mcp/secure"
SECURE_SERVER_URI = SERVER_URI
SECURE_CLIENT_URI = CLIENT_URI
SECURE_USERNAME = "operator"
SECURE_PASSWORD = "hunter2"


def _node_major() -> int:
    """Major version of the `node` on PATH, or 0 if it cannot be determined."""
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30)
        return int(out.stdout.strip().lstrip("v").split(".")[0])
    except Exception:
        return 0


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _free_port() -> int:
    """A port nothing is listening on, for a mock this session will own.

    There is a race between closing this socket and the mock binding the port,
    but it is microseconds wide against a machine-local port space — far
    narrower than the certainty of collision that fixed ports gave us.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _await_listening(proc: subprocess.Popen, port: int, name: str, timeout: float = 60) -> None:
    """Block until `proc` is accepting connections on `port`, or fail loudly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(HOST, port):
            return
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited during startup")
        time.sleep(0.2)
    raise RuntimeError(f"{name} did not start within {timeout:.0f}s")


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def opcua_server() -> str:
    """Start the mock OPC UA server for this session on a port of its own.

    Yields the server endpoint URL.
    """
    if SERVER_URL_OVERRIDE:
        yield SERVER_URL_OVERRIDE
        return

    port = _free_port()
    url = f"opc.tcp://{HOST}:{port}{SERVER_PATH}"
    proc = subprocess.Popen(
        ["uv", "run", "--no-sync", "opcua-mock-server", "--endpoint", url],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_listening(proc, port, "mock OPC UA server")
        time.sleep(HISTORY_WARMUP_SECONDS)  # let some history build up
        yield url
    finally:
        _terminate(proc)


@pytest.fixture(scope="session")
def aggregate_opcua_server() -> str:
    """Start the aggregate-capable mock OPC UA server on a port of its own.

    Skips the dependent tests when the mock's dependencies are not installed,
    mirroring how the Node tests skip on a missing build.

    Yields the server endpoint URL.
    """
    if AGGREGATE_SERVER_URL_OVERRIDE:
        yield AGGREGATE_SERVER_URL_OVERRIDE
        return

    if not (AGGREGATE_MOCK_DIR / "node_modules").is_dir():
        pytest.skip(
            "aggregate mock not installed — run `npm install` in packages/mock-server-aggregate"
        )

    if _node_major() < AGGREGATE_MOCK_MIN_NODE:
        pytest.skip(
            f"aggregate mock needs Node >={AGGREGATE_MOCK_MIN_NODE} "
            f"(node-opcua-aggregates pulls @peculiar/x509, which requires it); "
            f"found Node {_node_major()}"
        )

    port = _free_port()
    proc = subprocess.Popen(
        ["node", "server.mjs"],
        cwd=AGGREGATE_MOCK_DIR,
        env={**os.environ, "AGGREGATE_MOCK_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_listening(proc, port, "aggregate mock OPC UA server")
        time.sleep(AGGREGATE_WARMUP_SECONDS)  # let history build up to aggregate over
        yield f"opc.tcp://{HOST}:{port}{AGGREGATE_PATH}"
    finally:
        _terminate(proc)


@pytest.fixture(scope="session")
def secure_pki(tmp_path_factory) -> dict[str, str]:
    """Freshly generated server and client key pairs for the secured mock.

    One client certificate serves both runtimes: each announces its
    subjectAltName URI as the session's ApplicationUri, whether or not
    `OPCUA_APPLICATION_URI` names it, and the secured mock checks that they do.
    """
    directory = tmp_path_factory.mktemp("pki")
    server_cert, server_key = write_self_signed(directory, "server", SECURE_SERVER_URI)
    client_cert, client_key = write_self_signed(directory, "client", SECURE_CLIENT_URI)
    return {
        "server_cert": str(server_cert),
        "server_key": str(server_key),
        "client_cert": str(client_cert),
        "client_key": str(client_key),
    }


@pytest.fixture(scope="session")
def secure_opcua_server(secure_pki) -> str:
    """Start the secured mock OPC UA server for the session on a port of its own.

    Yields the server endpoint URL.
    """
    port = _free_port()
    url = f"opc.tcp://{HOST}:{port}{SECURE_PATH}"
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            str(ROOT / "tests" / "fixtures" / "secure_opcua_server.py"),
            "--endpoint",
            url,
            "--cert",
            secure_pki["server_cert"],
            "--key",
            secure_pki["server_key"],
            "--uri",
            SECURE_SERVER_URI,
            # As real equipment does: a session whose announced ApplicationUri is
            # not the one in the certificate it presented is refused, so the
            # tests can tell a correctly derived ApplicationUri from a library
            # default that would otherwise connect just as happily.
            "--check-client-uri",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_listening(proc, port, "secured mock OPC UA server")
        yield url
    finally:
        _terminate(proc)
