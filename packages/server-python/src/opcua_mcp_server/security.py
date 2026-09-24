"""OPC UA connection security, configured from the environment.

The defaults are ``None``/``None`` — unencrypted and unauthenticated — which is
what the bundled mock expects and what every release up to 0.2.1 hardcoded.
Anything pointed at real equipment should set at least a policy and a mode.

The Node runtime mirrors this module (``packages/server-node/src/security.ts``):
same variables, same defaults, same error wording, so a config that works
against one runtime works against the other. The one deliberate difference is
the set of policies: ``node-opcua`` implements two AES suites that
``python-opcua`` does not, and asking for one here is an error rather than a
silent downgrade.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from cryptography import x509
from opcua import Client, ua
from opcua.crypto import security_policies, uacrypto

from .transport_limits import advertise_limits

#: Policies this runtime can negotiate — the four both runtimes share.
POLICIES = ("None", "Basic128Rsa15", "Basic256", "Basic256Sha256")

#: Policies only the Node runtime implements, so the error can name them as such.
NODE_ONLY_POLICIES = ("Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss")

MODES = ("None", "Sign", "SignAndEncrypt")

_MESSAGE_SECURITY_MODES = {
    # `None` is a Python keyword, so python-opcua spells that member `None_`.
    "None": ua.MessageSecurityMode.None_,
    "Sign": ua.MessageSecurityMode.Sign,
    "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt,
}


@dataclass(frozen=True)
class SecurityConfig:
    """Resolved connection security: what to negotiate and who to log in as."""

    policy: str
    mode: str
    #: Client certificate, required for any policy other than ``None``.
    client_cert: str | None = None
    #: Private key for ``client_cert``.
    client_key: str | None = None
    #: Application URI announced to the server. OPC UA servers may reject a session
    #: whose ApplicationDescription URI does not match the ``subjectAltName`` URI of
    #: the client certificate, so it has to be settable alongside the certificate.
    #: ``None`` means "take it from the certificate" — see :func:`create_client`.
    application_uri: str | None = None
    #: Username identity; anonymous when ``None``.
    username: str | None = None
    #: Password for ``username``. May be empty, so ``None`` — not ``""`` — means unset.
    password: str | None = None
    #: The OPC UA *server* certificate this client expects, pinned. Unset, both
    #: client libraries take whatever certificate the endpoint presents and
    #: encrypt to it — protection against passive eavesdropping, but not against
    #: whoever managed to answer. Set, a server presenting anything else cannot
    #: complete the handshake.
    server_cert: str | None = None
    #: Certificate identifying the *user*, for X.509 authentication. Deliberately
    #: not ``client_cert``: that one secures the channel and is the application's
    #: identity, this one is the user's, is a different key pair, and is what the
    #: server checks against its user list. Conflating the two is the obvious way
    #: to get this wrong, so they are named apart and validated apart.
    user_cert: str | None = None
    #: Private key for ``user_cert``. Signs the server's challenge; never sent.
    user_key: str | None = None


def _read(env: Mapping[str, str], name: str) -> str | None:
    """The variable's value, or None when unset or blank.

    Blank counts as unset because MCP client configs routinely carry empty
    ``env`` entries, and ``OPCUA_SECURITY_POLICY=""`` clearly means "not
    configured".
    """
    value = env.get(name)
    value = value.strip() if value else ""
    return value or None


def _canonicalize(names: tuple[str, ...], value: str) -> str | None:
    """The entry of ``names`` matching ``value`` case-insensitively, if any."""
    return next((name for name in names if name.lower() == value.lower()), None)


def parse_security_config(
    env: Mapping[str, str],
    exists: Callable[[str], bool] = os.path.exists,
) -> SecurityConfig:
    """Read and validate the security configuration from an environment.

    Raises ``ValueError`` on any combination OPC UA cannot honour, rather than
    letting it fail later as an opaque library error against real equipment.
    ``exists`` is injectable so the parser stays testable without real files.
    """
    raw_policy = _read(env, "OPCUA_SECURITY_POLICY")
    raw_mode = _read(env, "OPCUA_SECURITY_MODE")

    policy = "None"
    if raw_policy is not None:
        match = _canonicalize(POLICIES, raw_policy)
        if match is None:
            node_only = _canonicalize(NODE_ONLY_POLICIES, raw_policy)
            if node_only:
                raise ValueError(
                    f'Security policy "{node_only}" is supported only by the Node runtime; '
                    f"the Python runtime (python-opcua) supports: {', '.join(POLICIES)}"
                )
            raise ValueError(
                f'Invalid OPCUA_SECURITY_POLICY: "{raw_policy}". Use one of: {", ".join(POLICIES)}'
            )
        policy = match

    mode = None if raw_mode is None else _canonicalize(MODES, raw_mode)
    if raw_mode is not None and mode is None:
        raise ValueError(
            f'Invalid OPCUA_SECURITY_MODE: "{raw_mode}". Use one of: {", ".join(MODES)}'
        )

    if policy == "None" and mode is not None and mode != "None":
        raise ValueError(
            f"OPCUA_SECURITY_MODE={mode} requires OPCUA_SECURITY_POLICY to be set to a policy "
            f"other than None"
        )
    if policy != "None" and mode == "None":
        raise ValueError(
            f"OPCUA_SECURITY_POLICY={policy} cannot be combined with OPCUA_SECURITY_MODE=None; "
            f"use Sign or SignAndEncrypt"
        )

    # A policy on its own implies the strongest mode it can carry: asking for
    # encryption and getting only signing would be a silent downgrade.
    if mode is None:
        mode = "None" if policy == "None" else "SignAndEncrypt"

    client_cert = _read(env, "OPCUA_CLIENT_CERT")
    client_key = _read(env, "OPCUA_CLIENT_KEY")
    if policy != "None" and not (client_cert and client_key):
        raise ValueError(
            f"OPCUA_SECURITY_POLICY={policy} requires OPCUA_CLIENT_CERT and OPCUA_CLIENT_KEY "
            f"(paths to the client certificate and its private key)"
        )
    for name, path in (("OPCUA_CLIENT_CERT", client_cert), ("OPCUA_CLIENT_KEY", client_key)):
        if path and not exists(path):
            raise ValueError(f"{name} does not exist: {path}")

    application_uri = _read(env, "OPCUA_APPLICATION_URI")

    # --- server certificate verification ---------------------------------------
    server_cert = _read(env, "OPCUA_SERVER_CERT")
    if server_cert is not None and policy == "None":
        # Refused rather than ignored. With no channel security the server's
        # certificate is never exchanged, so pinning it would verify nothing
        # while reading, in a config file, exactly like protection. A security
        # control that silently does nothing is worse than its absence.
        raise ValueError(
            "OPCUA_SERVER_CERT requires OPCUA_SECURITY_POLICY to be set to a policy other "
            "than None; with no channel security the server presents no certificate to verify"
        )

    # --- X.509 user authentication ----------------------------------------------
    user_cert = _read(env, "OPCUA_USER_CERT")
    user_key = _read(env, "OPCUA_USER_KEY")
    if (user_cert is None) != (user_key is None):
        raise ValueError(
            "OPCUA_USER_CERT and OPCUA_USER_KEY must be set together (the user's certificate "
            "and the private key that signs the server's challenge)"
        )
    if user_cert is not None and policy == "None":
        raise ValueError(
            "OPCUA_USER_CERT requires OPCUA_SECURITY_POLICY to be set to a policy other than "
            "None; the certificate challenge is signed over the server certificate, which an "
            "unsecured channel does not carry"
        )

    for name, path in (
        ("OPCUA_SERVER_CERT", server_cert),
        ("OPCUA_USER_CERT", user_cert),
        ("OPCUA_USER_KEY", user_key),
    ):
        if path and not exists(path):
            raise ValueError(f"{name} does not exist: {path}")

    username = _read(env, "OPCUA_USERNAME")
    if user_cert is not None and username is not None:
        # One session carries one user identity token. Accepting both would mean
        # choosing one silently, and the one not chosen is the one the operator
        # thinks is in force.
        raise ValueError(
            "OPCUA_USER_CERT cannot be combined with OPCUA_USERNAME; a session has one user "
            "identity, so use either certificate or username authentication"
        )
    # Not `_read`: an empty password is a real (if unwise) credential, so only an
    # absent variable counts as unset — except when there is no username to pair
    # it with, where a blank value can only mean "not configured". MCP client
    # configs routinely carry empty env entries, and an MCP bundle substitutes an
    # unset optional field as an empty string, so treating that pair as a usage
    # error would make an anonymous connection impossible to express there.
    password = env.get("OPCUA_PASSWORD")
    if username is None and not password:
        password = None
    if username is not None and password is None:
        raise ValueError("OPCUA_USERNAME requires OPCUA_PASSWORD")
    if username is None and password is not None:
        raise ValueError("OPCUA_PASSWORD requires OPCUA_USERNAME")

    return SecurityConfig(
        policy=policy,
        mode=mode,
        client_cert=client_cert,
        client_key=client_key,
        application_uri=application_uri,
        username=username,
        password=password,
        server_cert=server_cert,
        user_cert=user_cert,
        user_key=user_key,
    )


@lru_cache(maxsize=1)
def security_config() -> SecurityConfig:
    """The process-wide security configuration, parsed once."""
    return parse_security_config(os.environ)


def describe_security(config: SecurityConfig) -> str:
    """One-line, secret-free summary for the startup log."""
    if config.user_cert is not None:
        user = "certificate"
    elif config.username is None:
        user = "anonymous"
    else:
        user = f'"{config.username}"'
    # ``server-cert=`` only when pinning is on: a line that said ``pinned`` vs
    # ``unpinned`` on every startup would train the reader to skip it, and this
    # is the one word that distinguishes "encrypted" from "encrypted to whoever
    # answered".
    pinned = "" if config.server_cert is None else " server-cert=pinned"
    return f"policy={config.policy} mode={config.mode} user={user}{pinned}"


def security_warnings(config: SecurityConfig) -> list[str]:
    """Warnings to log before connecting; empty once a policy is configured.

    Keyed on the policy alone, never on the presence of a user: a username
    authenticates the session but leaves every read, write and method call on
    the wire in the clear, so credentials must not buy silence here. The
    password may be among what is in the clear — both client libraries send it
    unencrypted when the server's user-token policy specifies no security policy
    of its own (python-opcua logs "Sending plain-text password" when it does).
    """
    if config.policy != "None":
        # Encryption without a pinned server certificate is encryption to
        # whoever answered — DNS, ARP, a compromised switch or a mistyped
        # endpoint all reach it. Said once, on a secured connection, because
        # this is the gap a reader of ``policy=Basic256Sha256`` is least likely
        # to suspect.
        if config.server_cert is None:
            return [
                "the OPC UA server's certificate is not being verified — set OPCUA_SERVER_CERT "
                "to pin it. Encryption without it protects against passive eavesdropping, not "
                "against an attacker who can impersonate the endpoint, so control tools stay "
                "disabled unless OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true."
            ]
        return []

    warnings = [
        "connecting with no OPC UA security (policy=None) — traffic is unencrypted and "
        "unsigned. Set OPCUA_SECURITY_POLICY for anything beyond local development."
    ]
    if config.username is not None:
        warnings.append(
            "OPCUA_USERNAME/OPCUA_PASSWORD are being sent over that unencrypted channel, and "
            "the password is in clear text unless the server's user-token policy encrypts it."
        )
    return warnings


def _iso_second(moment: datetime) -> str:
    """A validity bound as the refusal words it: ISO-8601 UTC, to the second.

    ``security.ts`` formats the same instant the same way, so the refusal reads
    identically on both runtimes.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pinned_certificate_problem(path: str, now: datetime | None = None) -> str | None:
    """Why the pinned server certificate cannot vouch for the server, or None.

    Checked at every connect rather than once at startup, because a certificate
    expires while a process runs. Neither client library looks at the validity
    window of a pinned certificate — python-opcua only encrypts to its key — so
    without this an expired pin would keep "verifying" a server whose identity
    document its owner has retired.

    Fails closed: the connection is refused with this as the reason, rather than
    carrying on unverified, because the operator said which server this is and
    the evidence for it is no longer valid. An unreadable file is left to the
    library, which refuses it in its own words.

    ``pinnedCertificateProblem`` in ``security.ts`` is the other half.
    """
    try:
        certificate = uacrypto.load_certificate(path)
    except Exception:
        return None
    moment = now or datetime.now(timezone.utc)
    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc
    if moment > not_after:
        return (
            f"OPCUA_SERVER_CERT {path} expired on {_iso_second(not_after)}, so it cannot "
            f"verify the server. Pin the certificate the server presents now, renewing it on "
            f"the server first if that is the one that expired."
        )
    if moment < not_before:
        return (
            f"OPCUA_SERVER_CERT {path} is not valid until {_iso_second(not_before)}, so it "
            f"cannot verify the server yet. Check this machine's clock, or pin the certificate "
            f"the server presents now."
        )
    return None


def certificate_application_uri(path: str) -> str | None:
    """The ``subjectAltName`` URI of the certificate at ``path``, if it has one.

    This is the ApplicationUri an OPC UA client is supposed to announce: servers
    check the ApplicationDescription of a session against the URI in the
    certificate it presented, and reject the mismatch with
    ``BadCertificateUriInvalid``. node-opcua reads it out of the certificate
    itself; python-opcua never looks, and announces ``urn:freeopcua:client``
    unless told otherwise — so without this, the same certificate and the same
    variables reach a real server as two different identities depending on which
    runtime you started.

    PEM or DER is decided by the file extension, because that is the rule
    python-opcua applies to this very file when it loads it for the handshake
    (``.pem`` is PEM, anything else is DER). Returns ``None`` rather than
    raising if the file cannot be read or carries no URI: this is a best-effort
    default, and a certificate python-opcua cannot use fails the connection
    itself, in its own words.
    """
    try:
        certificate = uacrypto.load_certificate(path)
        alt_names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        uris = alt_names.value.get_values_for_type(x509.UniformResourceIdentifier)
    except Exception:  # unreadable, not a certificate, or no subjectAltName
        return None
    return uris[0] if uris else None


#: Messages already written to stderr, so that the capability probes — which
#: build a client of their own before the server starts — do not repeat them.
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Log to stderr the first time this message comes up. stdout is the MCP transport."""
    if message not in _warned:
        _warned.add(message)
        print(f"WARNING: {message}", file=sys.stderr)


def create_client(url: str) -> Client:
    """An OPC UA client for ``url``, configured with the process's security.

    Not connected yet — but note that for a secured connection python-opcua
    fetches the server's certificate from its endpoint list here, so this does
    talk to the server. Call it off the event loop.
    """
    client = Client(url)
    # Before anything is sent: these go out in the Hello, and python-opcua's own
    # defaults are 0, which tells the server this client will accept a message of
    # any size in any number of chunks.
    advertise_limits(client)
    config = security_config()

    certificate_uri = (
        certificate_application_uri(config.client_cert) if config.client_cert else None
    )
    if config.application_uri is not None:
        client.application_uri = config.application_uri
        if certificate_uri is not None and certificate_uri != config.application_uri:
            _warn_once(
                f"OPCUA_APPLICATION_URI={config.application_uri} does not match the "
                f"subjectAltName URI of OPCUA_CLIENT_CERT ({certificate_uri}); a server that "
                f"checks the two will reject the session with BadCertificateUriInvalid. Unset "
                f"OPCUA_APPLICATION_URI to announce the certificate's own URI."
            )
    elif certificate_uri is not None:
        # The certificate is the authority on this, and the operator has not
        # said otherwise. Matches what node-opcua does with the same files.
        client.application_uri = certificate_uri

    if config.server_cert is not None:
        problem = pinned_certificate_problem(config.server_cert)
        if problem is not None:
            raise ValueError(problem)

    if config.policy != "None":
        policy = getattr(security_policies, f"SecurityPolicy{config.policy}")
        client.set_security(
            policy,
            config.client_cert,
            config.client_key,
            # Pinning, and an optimisation for free: given the certificate,
            # python-opcua skips the extra endpoint round-trip it otherwise makes
            # to fetch one. Left None, it takes whatever the endpoint presents.
            server_certificate_path=config.server_cert,
            mode=_MESSAGE_SECURITY_MODES[config.mode],
        )

    if config.user_cert is not None and config.user_key is not None:
        # X.509 user identity: python-opcua signs the server's challenge with
        # this key in `activate_session` and sends only the certificate. A
        # different key pair from `client_cert`, which secures the channel.
        client.load_client_certificate(config.user_cert)
        client.load_private_key(config.user_key)
    elif config.username is not None:
        client.set_user(config.username)
        client.set_password(config.password or "")

    return client
