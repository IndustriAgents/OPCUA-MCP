"""Shared pytest fixtures for the OPC UA MCP end-to-end suite.

The suite drives the *actual* MCP servers (Python and Node) over stdio using the
official `mcp` client SDK, pointed at the mock industrial OPC UA server. A single
session-scoped fixture makes sure a mock server is available: if one is already
listening on :4840 it is reused, otherwise one is started for the test session.

A second, aggregate-capable mock (`packages/mock-server-aggregate`, :4841) backs
the aggregate tests. It is kept separate from the main mock on purpose: the main
mock must keep advertising *no* aggregate functions so the suite can assert that
both MCP servers hide `read_aggregate_opcua_node` when it is unsupported.

A third, *secured* mock (`fixtures/secure_opcua_server.py`, :4843) backs the
connection-security tests. It is separate for the same reason: it offers no
unsecured endpoint at all, which is what makes those assertions mean something.
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
SERVER_URL = os.environ.get("OPCUA_SERVER_URL", "opc.tcp://localhost:4840/freeopcua/server/")
HOST = "localhost"
PORT = 4840

# Seconds of runtime to allow the mock server to accumulate history before tests
# read it back. The mock writes one history record per variable per second.
HISTORY_WARMUP_SECONDS = 6

# --- aggregate-capable mock (packages/mock-server-aggregate) --------------------
AGGREGATE_PORT = 4841
AGGREGATE_SERVER_URL = os.environ.get(
    "OPCUA_AGGREGATE_SERVER_URL", f"opc.tcp://localhost:{AGGREGATE_PORT}/UA/Aggregate"
)
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
SECURE_PORT = 4843
SECURE_SERVER_URL = f"opc.tcp://127.0.0.1:{SECURE_PORT}/mcp/secure"
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


@pytest.fixture(scope="session")
def opcua_server() -> str:
    """Ensure a mock OPC UA server is reachable; reuse an existing one if present.

    Yields the server endpoint URL.
    """
    if _port_open(HOST, PORT):
        # Something is already serving on :4840 — assume it is the mock server.
        yield SERVER_URL
        return

    proc = subprocess.Popen(
        ["uv", "run", "--no-sync", "opcua-mock-server"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if _port_open(HOST, PORT):
                break
            if proc.poll() is not None:
                raise RuntimeError("mock OPC UA server exited during startup")
            time.sleep(1)
        else:
            raise RuntimeError("mock OPC UA server did not start within 60s")

        time.sleep(HISTORY_WARMUP_SECONDS)  # let some history build up
        yield SERVER_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def aggregate_opcua_server() -> str:
    """Ensure the aggregate-capable mock OPC UA server is reachable.

    Reuses an instance already listening on :4841, otherwise starts one for the
    session. Skips the dependent tests when the mock's dependencies are not
    installed, mirroring how the Node tests skip on a missing build.

    Yields the server endpoint URL.
    """
    if _port_open(HOST, AGGREGATE_PORT):
        # Something is already serving on :4841 — assume it is the aggregate mock.
        yield AGGREGATE_SERVER_URL
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

    proc = subprocess.Popen(
        ["node", "server.mjs"],
        cwd=AGGREGATE_MOCK_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if _port_open(HOST, AGGREGATE_PORT):
                break
            if proc.poll() is not None:
                raise RuntimeError("aggregate mock OPC UA server exited during startup")
            time.sleep(1)
        else:
            raise RuntimeError("aggregate mock OPC UA server did not start within 60s")

        time.sleep(AGGREGATE_WARMUP_SECONDS)  # let history build up to aggregate over
        yield AGGREGATE_SERVER_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def secure_pki(tmp_path_factory) -> dict[str, str]:
    """Freshly generated server and client key pairs for the secured mock.

    One client certificate serves both runtimes: they announce its
    subjectAltName URI through `OPCUA_APPLICATION_URI`, which is exactly what
    that variable is for.
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
    """Start the secured mock OPC UA server for the session.

    Unlike the unsecured mocks this one is never reused from a previous run: the
    certificates are generated per session, so an already-running instance would
    be presenting a different one.

    Yields the server endpoint URL.
    """
    if _port_open("127.0.0.1", SECURE_PORT):
        pytest.skip(
            f"port {SECURE_PORT} is already in use — stop whatever is listening on it "
            f"so the secured mock can present this session's certificate"
        )

    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            str(ROOT / "tests" / "fixtures" / "secure_opcua_server.py"),
            "--endpoint",
            SECURE_SERVER_URL,
            "--cert",
            secure_pki["server_cert"],
            "--key",
            secure_pki["server_key"],
            "--uri",
            SECURE_SERVER_URI,
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if _port_open("127.0.0.1", SECURE_PORT):
                break
            if proc.poll() is not None:
                raise RuntimeError("secured mock OPC UA server exited during startup")
            time.sleep(1)
        else:
            raise RuntimeError("secured mock OPC UA server did not start within 60s")

        yield SECURE_SERVER_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
