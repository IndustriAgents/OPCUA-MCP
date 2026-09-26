"""Throwaway certificates, an optional lab server process, and endpoint discovery.

Against a vendor endpoint the harness only needs certificates. Against a lab
server — one built from an SDK on this machine, as the published results are —
it also starts the server itself, because two scenarios cannot be run any other
way: a restart while a session is open, and a restart that reorders the
server's namespaces. Nothing here reaches a result file except what discovery
says the server offers (policies, modes, token types), which is exactly the
"enabled features" a result has to record.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fixtures.pki import write_self_signed

from conformance.config import Config, ConfigError, resolve
from conformance.runtime import ROOT

CLIENT_URI = "urn:opcua-mcp:conformance-client"
USER_URI = "urn:opcua-mcp:conformance-user"


def _write_der(pem_path: Path) -> Path:
    """A DER copy next to a PEM certificate or key: some servers take only DER."""
    data = pem_path.read_bytes()
    der_path = pem_path.with_suffix(".der")
    if b"PRIVATE KEY" in data:
        key = serialization.load_pem_private_key(data, password=None)
        der_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    else:
        cert = x509.load_pem_x509_certificate(data)
        der_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    return der_path


@dataclass
class Pki:
    """Every certificate a run uses, generated fresh into one directory.

    `client` is the application identity the server is expected to trust;
    `untrusted` is an equally valid one it has never seen, for the negative
    case; `impostor` is a server certificate that is not the server's, for the
    pinning case; `user` is an X.509 *user* identity.
    """

    directory: Path
    files: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def generate(cls, directory: Path, server_uri: str) -> Pki:
        directory.mkdir(parents=True, exist_ok=True)
        pki = cls(directory)
        for name, uri in (
            ("server", server_uri),
            ("client", CLIENT_URI),
            ("untrusted", CLIENT_URI),
            ("impostor", server_uri),
            ("user", USER_URI),
        ):
            cert, key = write_self_signed(directory, name, uri)
            pki.files[name] = cert
            pki.files[f"{name}_key"] = key
            pki.files[f"{name}_der"] = _write_der(cert)
            pki.files[f"{name}_key_der"] = _write_der(key)
        return pki

    def __getitem__(self, name: str) -> str:
        return str(self.files[name])


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


class LabServer:
    """A server process the harness owns, so it can take it away and back."""

    def __init__(self, config: Config, pki: Pki, state: Path) -> None:
        lab = config.lab or {}
        self.config = config
        self.pki = pki
        self.state = state
        self.url = config.endpoint_url()
        parsed = urlparse(self.url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 4840
        self.startup_seconds = float(lab.get("startupSeconds", 60))
        self.warmup_seconds = float(lab.get("warmupSeconds", 0))
        self.log = state / "lab-server.log"
        self._proc: subprocess.Popen | None = None
        # Where the server binary or classpath lives differs per machine, so a
        # command names it through a variable an env reference fills in.
        # Relative paths resolve against the repository root, the command's cwd.
        self._variables = {
            name: resolve(value) or "" for name, value in lab.get("variables", {}).items()
        }

    def _expand(self, argv: list[str]) -> list[str]:
        values = {
            "pki": str(self.pki.directory),
            "state": str(self.state),
            "port": str(self.port),
            "root": str(ROOT),
            **self._variables,
        }
        try:
            return [arg.format(**values) for arg in argv]
        except KeyError as missing:
            raise ConfigError(f"lab.command: unknown placeholder {missing}") from missing

    def prepare(self) -> None:
        """Put the trusted client and user certificates where the server looks."""
        for directory in (self.config.lab or {}).get("trustDirectories", []):
            target = Path(self._expand([directory])[0])
            target.mkdir(parents=True, exist_ok=True)
            for name in ("client_der", "user_der"):
                shutil.copy(self.pki.files[name], target / f"{name}.der")

    def start(self, variant: str = "command") -> None:
        argv = (self.config.lab or {}).get(variant)
        if not argv:
            raise ConfigError(f"lab.{variant} is not configured")
        if _port_open(self.host, self.port):
            raise RuntimeError(
                f"something is already listening on port {self.port}; stop it first — "
                "the harness must own the lab server it restarts"
            )
        with self.log.open("a") as log:
            self._proc = subprocess.Popen(
                self._expand(argv),
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.time() + self.startup_seconds
        while time.time() < deadline:
            if _port_open(self.host, self.port):
                return
            if self._proc.poll() is not None:
                raise RuntimeError(f"lab server exited during startup; see {self.log}")
            time.sleep(0.25)
        raise RuntimeError(f"lab server did not listen within {self.startup_seconds:.0f}s")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=15)
        self._proc = None
        deadline = time.time() + 30
        while time.time() < deadline and _port_open(self.host, self.port):
            time.sleep(0.2)

    def restart(self, variant: str = "command") -> None:
        self.stop()
        self.start(variant)

    def can(self, variant: str) -> bool:
        return bool((self.config.lab or {}).get(variant))


# --- endpoint discovery ----------------------------------------------------------

_MODE_NAMES = {1: "None", 2: "Sign", 3: "SignAndEncrypt"}
_TOKEN_NAMES = {0: "Anonymous", 1: "UserName", 2: "Certificate", 3: "IssuedToken"}


@dataclass
class Discovery:
    """What GetEndpoints said, reduced to what is safe to publish."""

    endpoints: list[dict[str, Any]]
    server_certificate: Path | None
    error: str | None = None

    def offers(self, policy: str, mode: str) -> bool:
        return any(e["policy"] == policy and e["mode"] == mode for e in self.endpoints)

    def token_types(self) -> set[str]:
        return {token for e in self.endpoints for token in e["user_tokens"]}


def discover(url: str, pki: Pki) -> Discovery:
    """Ask the server for its endpoints, with the harness's own OPC UA client.

    This is the harness's reconnaissance, not a scenario: it uses python-opcua
    directly because it has to know what the server offers before it can say
    whether a runtime that failed to use it failed or was never asked to.
    """
    from opcua import Client

    try:
        client = Client(url, timeout=10)
        endpoints = client.connect_and_get_server_endpoints()
    except Exception as error:
        return Discovery([], None, f"{type(error).__name__}: {error}")

    summary: list[dict[str, Any]] = []
    certificate: bytes | None = None
    for endpoint in endpoints:
        policy = endpoint.SecurityPolicyUri.rsplit("#", 1)[-1]
        entry = {
            "policy": policy,
            "mode": _MODE_NAMES.get(int(endpoint.SecurityMode), str(endpoint.SecurityMode)),
            "user_tokens": sorted(
                {
                    _TOKEN_NAMES.get(int(t.TokenType), str(t.TokenType))
                    for t in endpoint.UserIdentityTokens
                }
            ),
        }
        if entry not in summary:
            summary.append(entry)
        if endpoint.ServerCertificate and certificate is None:
            certificate = endpoint.ServerCertificate

    cert_path = None
    if certificate:
        cert_path = pki.directory / "server-discovered.pem"
        loaded = x509.load_der_x509_certificate(certificate)
        cert_path.write_bytes(loaded.public_bytes(serialization.Encoding.PEM))
    return Discovery(sorted(summary, key=lambda e: (e["policy"], e["mode"])), cert_path)
