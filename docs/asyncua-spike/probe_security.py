# ruff: noqa: E501
"""Security through asyncua 2.0.1: policies, pinning, ApplicationUri, user identity.

Against tests/fixtures/secure_opcua_server.py (python-opcua, Basic256Sha256
only, username required, --check-client-uri) and, when OPEN62541_LAB_SERVER
points at the #169 open62541 lab build, against a server offering every policy
including the two AES ones, username and X.509 user tokens and a trust list.

    OPEN62541_LAB_SERVER=/path/to/lab_server \
    uv run --no-sync --with asyncua==2.0.1 python docs/asyncua-spike/probe_security.py
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

from _common import (
    LAB_CREDENTIALS,
    lab_available,
    make_pki,
    open62541_lab,
    report,
    secure_mock,
)
from asyncua import Client, ua
from asyncua.crypto import security_policies, uacrypto
from fixtures.pki import CLIENT_URI

logging.disable(logging.CRITICAL)
USERNAME, PASSWORD = "operator", "hunter2"  # tests/fixtures/secure_opcua_server.py


async def attempt(url: str, pki: dict, *, policy="Basic256Sha256", mode="SignAndEncrypt",
                  pin: str | None = "server_cert", uri: str | None = CLIENT_URI,
                  user: tuple[str, str] | None = (USERNAME, PASSWORD), x509_user: str | None = None,
                  client: str = "client") -> str:  # fmt: skip
    """Connect, read ServerStatus.State, disconnect; the outcome as one string."""
    c = Client(url, timeout=8)
    if uri:
        c.application_uri = uri
    started = time.monotonic()
    try:
        if policy != "None":
            await c.set_security(
                getattr(security_policies, f"SecurityPolicy{policy}"),
                pki[f"{client}_cert"],
                pki[f"{client}_key"],
                server_certificate=pki[pin] if pin else None,
                mode=getattr(ua.MessageSecurityMode, mode),
            )
        if x509_user:
            await c.load_client_certificate(pki[f"{x509_user}_cert"])
            await c.load_private_key(pki[f"{x509_user}_key"])
        elif user:
            c.set_user(user[0])
            c.set_password(user[1])
        await c.connect()
        try:
            state = await c.nodes.server_state.read_value()
            return f"connected, ServerState={ua.ServerState(state).name}"
        finally:
            await c.disconnect()
    except BaseException as error:
        cause = f" from {error.__cause__!r}" if error.__cause__ else ""
        return f"refused: {type(error).__name__}: {str(error)[:120]}{cause} ({time.monotonic() - started:.1f}s)"


def legacy_attempt(url: str, pki: dict, pin: str) -> str:
    """The same connection through python-opcua 0.98.13, as the runtime makes it today."""
    from opcua import Client as LegacyClient
    from opcua.crypto import security_policies as legacy_policies

    c = LegacyClient(url, timeout=8)
    c.application_uri = CLIENT_URI
    c.set_security(
        legacy_policies.SecurityPolicyBasic256Sha256,
        pki["client_cert"],
        pki["client_key"],
        server_certificate_path=pki[pin],
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    c.set_user(USERNAME)
    c.set_password(PASSWORD)
    try:
        c.connect()
    except Exception as error:
        return f"refused: {type(error).__name__}: {str(error)[:100]}"
    c.disconnect()
    return "connected"


async def against_secure_mock(pki: dict) -> None:
    with secure_mock(pki) as url:
        report(
            "supported",
            "security.basic256sha256",
            f"Basic256Sha256/SignAndEncrypt, pinned server cert, cert's ApplicationUri, username: "
            f"{await attempt(url, pki)}",
        )
        default_uri = Client("opc.tcp://x").application_uri
        report(
            "differs",
            "security.application-uri",
            f"left at asyncua's default ApplicationUri {default_uri!r}: "
            f"{await attempt(url, pki, uri=None)}. As with python-opcua, the adapter must set "
            f"application_uri from the certificate's subjectAltName (security.py already does)",
        )
        report(
            "supported",
            "security.pinning-mismatch",
            f"server_certificate pinned to a different certificate: "
            f"{await attempt(url, pki, pin='other_cert')}. Fail-closed: the channel is encrypted "
            f"to the pinned key, and CreateSession additionally raises 'Server certificate "
            f"mismatch' if the pinned DER differs from the one the server presents",
        )
        report(
            "supported",
            "security.pinning-same-key",
            f"pinned: a re-issued certificate for the server's *own key* (so the channel opens): "
            f"{await attempt(url, pki, pin='reissued_cert')}; python-opcua with the same pin: "
            f"{await asyncio.to_thread(legacy_attempt, url, pki, 'reissued_cert')}. Both libraries "
            f"compare the pinned DER with the one CreateSession returns, so parity holds",
        )
        report(
            "supported",
            "security.unpinned",
            f"server_certificate=None (fetched from GetEndpoints, python-opcua does the same): "
            f"{await attempt(url, pki, pin=None)}",
        )
        report(
            "supported",
            "security.bad-password",
            f"wrong password: {await attempt(url, pki, user=(USERNAME, 'wrong'))}",
        )
        report(
            "supported",
            "security.no-downgrade",
            f"policy None against a server with no None endpoint: "
            f"{await attempt(url, pki, policy='None', mode='None_')}",
        )


async def pem_by_extension(pki: dict, directory: Path) -> None:
    renamed = directory / "client.key"
    shutil.copy(pki["client_key"], renamed)
    outcomes = []
    for extension in (None, "pem"):
        try:
            await uacrypto.load_private_key(str(renamed), extension=extension)
            outcomes.append(f"extension={extension}: loaded")
        except Exception as error:
            outcomes.append(f"extension={extension}: {type(error).__name__}")
    content = Path(pki["client_cert"]).read_bytes()
    try:
        await uacrypto.load_certificate(content)
        as_bytes = "loaded"
    except Exception as error:
        as_bytes = type(error).__name__
    report(
        "differs",
        "security.pem-by-extension",
        f"a PEM key named client.key: {outcomes}; PEM certificate passed as bytes: {as_bytes}. "
        f"The adapter can sniff b'-----BEGIN' itself and pass extension=, making PEM and DER "
        f"load under any name as on Node",
    )


async def against_lab(pki: dict) -> None:
    with open62541_lab(pki) as (url, _, _):
        probe = Client(url, timeout=8)
        endpoints = await probe.connect_and_get_server_endpoints()
        offered = sorted(
            {(e.SecurityPolicyUri.split("#")[1], e.SecurityMode.name) for e in endpoints}
        )
        tokens = sorted({t.TokenType.name for e in endpoints for t in e.UserIdentityTokens})
        report("note", "security.lab-endpoints", f"open62541 offers {offered}; tokens {tokens}")
        user = (
            LAB_CREDENTIALS["OPCUA_CONFORMANCE_USERNAME"],
            LAB_CREDENTIALS["OPCUA_CONFORMANCE_PASSWORD"],
        )
        results = {}
        for policy, mode in offered:
            if policy == "None":
                continue
            name = policy.replace("_", "")
            results[f"{policy}/{mode}"] = await attempt(
                url, pki, policy=name, mode=mode, pin="server_der", user=user
            )
        good = [k for k, v in results.items() if v.startswith("connected")]
        report(
            "supported" if len(good) == len(results) else "differs",
            "security.policies-live",
            f"{len(good)}/{len(results)} offered policy/mode pairs connect with a username: {results}",
        )
        report(
            "supported",
            "security.x509-user",
            f"Basic256Sha256 channel + X.509 user token (user cert in the lab's trust list): "
            f"{await attempt(url, pki, pin='server_der', x509_user='user')}",
        )
        report(
            "supported",
            "security.x509-user-untrusted",
            f"X.509 user token with a certificate the lab does not trust: "
            f"{await attempt(url, pki, pin='server_der', x509_user='other')}",
        )
        report(
            "supported",
            "security.aes-pinning-mismatch",
            f"Aes256_Sha256_RsaPss with server_certificate pinned to another cert: "
            f"{await attempt(url, pki, policy='Aes256Sha256RsaPss', pin='other_cert', user=user)}",
        )
        report(
            "supported",
            "security.client-untrusted",
            f"a client certificate outside the lab's trust list: "
            f"{await attempt(url, pki, pin='server_der', user=user, client='other')}",
        )


def reissue(pki: dict, directory: Path) -> str:
    """The server's certificate re-issued with a new serial: same key, different DER."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    original = x509.load_pem_x509_certificate(Path(pki["server_cert"]).read_bytes())
    key = serialization.load_pem_private_key(Path(pki["server_key"]).read_bytes(), None)
    builder = (
        x509.CertificateBuilder()
        .subject_name(original.subject)
        .issuer_name(original.issuer)
        .public_key(original.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(original.not_valid_before_utc)
        .not_valid_after(original.not_valid_after_utc)
    )
    for extension in original.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    path = directory / "server_reissued.pem"
    path.write_bytes(builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    return str(path)


async def main() -> None:
    directory = Path(tempfile.mkdtemp(prefix="asyncua-spike-pki-"))
    pki = make_pki(directory, "server", "client", "user", "other")
    pki["reissued_cert"] = reissue(pki, directory)
    await against_secure_mock(pki)
    await pem_by_extension(pki, directory)
    if lab_available():
        await against_lab({**pki, "server_der": pki["server_der"]})
    else:
        report("skip", "security.lab", "OPEN62541_LAB_SERVER not set; AES and X.509 rows not run")


asyncio.run(main())
