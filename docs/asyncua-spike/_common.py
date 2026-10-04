"""Shared plumbing for the asyncua compatibility probes (#144, step S0).

Each probe starts the repository's own mock servers (the same commands
tests/conftest.py uses), drives them with asyncua 2.0.1 and, where the
comparison matters, with python-opcua 0.98.13 as well, and prints one line per
finding:

    [supported] browse.continuation: 1 BrowseNext page ...
    [differs]   datetime.tz: asyncua returns aware UTC, python-opcua naive
    [missing]   ...
    [bug]       transport.ack-overwrite: ...

The verdicts are the probe's own reading of what it observed; the matrix in
docs/asyncua-compatibility.md is built from these lines. Nothing here is
imported by the runtime, and nothing here is a test the suite runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"
sys.path.insert(0, str(ROOT / "tests"))

#: The open62541 conformance lab server from #169, built by
#: compatibility/labs/open62541/build.sh on that branch. Optional: the probes
#: that need it skip with a note when it is absent.
OPEN62541_LAB = os.environ.get("OPEN62541_LAB_SERVER", "")

VERDICTS = ("supported", "differs", "missing", "bug", "note", "skip")


def report(verdict: str, row: str, detail: str) -> None:
    assert verdict in VERDICTS, verdict
    print(f"[{verdict}] {row}: {detail}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _await_port(proc: subprocess.Popen, port: int, name: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with {proc.returncode}")
        with contextlib.suppress(OSError), socket.create_connection((HOST, port), 0.5):
            return
        time.sleep(0.2)
    raise RuntimeError(f"{name} did not listen on {port}")


@contextlib.contextmanager
def _process(argv: list[str], port: int, name: str, cwd: Path, env: dict | None = None):
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_port(proc, port, name)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@contextlib.contextmanager
def python_mock(warmup: float = 0):
    """packages/mock-server: the python-opcua mock the e2e suite runs against."""
    port = free_port()
    url = f"opc.tcp://{HOST}:{port}/freeopcua/server/"
    argv = ["uv", "run", "--no-sync", "opcua-mock-server", "--endpoint", url]
    with _process(argv, port, "python-opcua mock", ROOT):
        time.sleep(warmup)
        yield url


@contextlib.contextmanager
def aggregate_mock(warmup: float = 0):
    """packages/mock-server-aggregate: node-opcua with ReadProcessed."""
    port = free_port()
    with _process(
        ["node", "server.mjs"],
        port,
        "aggregate mock",
        ROOT / "packages" / "mock-server-aggregate",
        {"AGGREGATE_MOCK_PORT": str(port)},
    ):
        time.sleep(warmup)
        yield f"opc.tcp://{HOST}:{port}/UA/Aggregate"


@contextlib.contextmanager
def alarm_mock():
    """packages/mock-server-alarms: node-opcua with one ExclusiveLimitAlarm."""
    port = free_port()
    with _process(
        ["node", "server.mjs"],
        port,
        "alarms mock",
        ROOT / "packages" / "mock-server-alarms",
        {"ALARM_MOCK_PORT": str(port)},
    ):
        yield f"opc.tcp://{HOST}:{port}/UA/Alarms"


@contextlib.contextmanager
def secure_mock(pki: dict[str, str]):
    """tests/fixtures/secure_opcua_server.py: Basic256Sha256 + username only."""
    from fixtures.pki import SERVER_URI

    port = free_port()
    url = f"opc.tcp://{HOST}:{port}/mcp/secure"
    argv = [
        "uv", "run", "--no-sync", "python",
        str(ROOT / "tests" / "fixtures" / "secure_opcua_server.py"),
        "--endpoint", url, "--cert", pki["server_cert"], "--key", pki["server_key"],
        "--uri", SERVER_URI, "--check-client-uri",
    ]  # fmt: skip
    with _process(argv, port, "secured mock", ROOT):
        yield url


def make_pki(directory: Path, *names: str) -> dict[str, str]:
    """PEM and DER copies of a self-signed pair per name, as fixtures/pki.py writes them."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from fixtures.pki import CLIENT_URI, SERVER_URI, write_self_signed

    uris = {"server": SERVER_URI, "client": CLIENT_URI}
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for name in names:
        cert, key = write_self_signed(directory, name, uris.get(name, f"urn:opcua-mcp:{name}"))
        files[f"{name}_cert"], files[f"{name}_key"] = str(cert), str(key)
        der = x509.load_pem_x509_certificate(cert.read_bytes()).public_bytes(
            serialization.Encoding.DER
        )
        (directory / f"{name}.der").write_bytes(der)
        private = serialization.load_pem_private_key(key.read_bytes(), None)
        (directory / f"{name}_key.der").write_bytes(
            private.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        files[f"{name}_der"] = str(directory / f"{name}.der")
        files[f"{name}_key_der"] = str(directory / f"{name}_key.der")
    return files


LAB_CREDENTIALS = {"OPCUA_CONFORMANCE_USERNAME": "labuser", "OPCUA_CONFORMANCE_PASSWORD": "labpass"}


@contextlib.contextmanager
def open62541_lab(pki: dict[str, str], pad: bool = False, port: int | None = None):
    """The #169 open62541 lab server: every policy incl. AES, username, X.509 users."""
    port = port or free_port()
    argv = [
        OPEN62541_LAB, "--port", str(port),
        "--cert", pki["server_der"], "--key", pki["server_key_der"],
        "--trust", pki["client_der"], "--trust", pki["user_der"],
    ]  # fmt: skip
    if pad:
        argv.append("--pad-namespace")
    with _process(argv, port, "open62541 lab", ROOT, LAB_CREDENTIALS) as proc:
        time.sleep(1)
        yield f"opc.tcp://{HOST}:{port}", port, proc


def lab_available() -> bool:
    return bool(OPEN62541_LAB) and Path(OPEN62541_LAB).is_file()


def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))
