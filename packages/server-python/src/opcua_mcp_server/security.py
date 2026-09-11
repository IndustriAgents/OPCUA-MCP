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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache

from opcua import Client, ua
from opcua.crypto import security_policies

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
    #: ``None`` leaves python-opcua's default (``urn:freeopcua:client``).
    application_uri: str | None = None
    #: Username identity; anonymous when ``None``.
    username: str | None = None
    #: Password for ``username``. May be empty, so ``None`` — not ``""`` — means unset.
    password: str | None = None


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

    username = _read(env, "OPCUA_USERNAME")
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
    )


@lru_cache(maxsize=1)
def security_config() -> SecurityConfig:
    """The process-wide security configuration, parsed once."""
    return parse_security_config(os.environ)


def describe_security(config: SecurityConfig) -> str:
    """One-line, secret-free summary for the startup log."""
    user = "anonymous" if config.username is None else f'"{config.username}"'
    return f"policy={config.policy} mode={config.mode} user={user}"


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


def create_client(url: str) -> Client:
    """An OPC UA client for ``url``, configured with the process's security.

    Not connected yet — but note that for a secured connection python-opcua
    fetches the server's certificate from its endpoint list here, so this does
    talk to the server. Call it off the event loop.
    """
    client = Client(url)
    config = security_config()

    if config.application_uri is not None:
        client.application_uri = config.application_uri

    if config.policy != "None":
        policy = getattr(security_policies, f"SecurityPolicy{config.policy}")
        client.set_security(
            policy,
            config.client_cert,
            config.client_key,
            mode=_MESSAGE_SECURITY_MODES[config.mode],
        )

    if config.username is not None:
        client.set_user(config.username)
        client.set_password(config.password or "")

    return client
