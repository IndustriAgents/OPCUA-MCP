"""End-to-end tests over a *secured* OPC UA connection.

Everything else in the suite talks to an unsecured mock, which cannot tell a
working security configuration from one that is silently ignored. These tests
drive both MCP servers against `fixtures/secure_opcua_server.py`, which offers
**only** Basic256Sha256 endpoints and requires a username — so a client that
negotiated no security, or presented no certificate, has nothing to connect to.

Covered here: the environment variables reaching node-opcua and python-opcua, an
encrypted channel, `Sign` as well as `SignAndEncrypt`, username/password
authentication, and the two failure modes an operator is most likely to hit.

**Not** covered, and no mock can cover it: a real server's certificate trust
list. python-opcua's server accepts any client certificate, whereas a Siemens,
Kepware or Prosys server rejects an unknown one until an operator moves it into
the trusted folder. Turning security on against real equipment stays a manual
step.

The servers report failures on stderr rather than over MCP, and the two runtimes
fail at different moments (see `test_a_wrong_password_is_rejected`), so these
tests capture the server's stderr and assert on the OPC UA status code in it.
Asserting only "the call did not succeed" would pass just as happily if the
server had died for an unrelated reason.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager

import pytest
from conftest import (
    ROOT,
    SECURE_CLIENT_URI,
    SECURE_PASSWORD,
    SECURE_SERVER_URI,
    SECURE_USERNAME,
)
from fixtures.pki import write_self_signed
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

NODE_BUILD = ROOT / "packages" / "server-node" / "build" / "index.js"

# The writable Double the secured mock publishes, as printed on its READY line.
TEMPERATURE = "ns=2;i=2"

CORE_TOOLS = {
    "read_opcua_nodes",
    "browse_opcua_nodes",
    "write_opcua_nodes",
    "call_opcua_method",
}


def _server_params(impl: str, url: str, env: dict[str, str]) -> StdioServerParameters:
    full_env = {
        **os.environ,
        "OPCUA_SERVER_URL": url,
        "OPCUA_PROFILE": "full",
        **env,
    }
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=full_env,
        )
    if impl == "node":
        return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=full_env)
    raise ValueError(impl)


@pytest.fixture(params=["python", "node"])
def impl(request) -> str:
    """Each test runs against both runtimes, as the rest of the e2e suite does."""
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip(
            "Node server not built — run `npm install && npm run build` in packages/server-node"
        )
    return request.param


@pytest.fixture
def secure_env(secure_pki) -> dict[str, str]:
    """A working security configuration for the secured mock."""
    return {
        "OPCUA_SECURITY_POLICY": "Basic256Sha256",
        "OPCUA_CLIENT_CERT": secure_pki["client_cert"],
        "OPCUA_CLIENT_KEY": secure_pki["client_key"],
        "OPCUA_APPLICATION_URI": SECURE_CLIENT_URI,
        "OPCUA_USERNAME": SECURE_USERNAME,
        "OPCUA_PASSWORD": SECURE_PASSWORD,
    }


@pytest.fixture
def errlog():
    """A file to collect the MCP server's stderr, which is where it reports."""
    with tempfile.TemporaryFile("w+", errors="replace") as handle:
        yield handle


def stderr_of(errlog) -> str:
    errlog.seek(0)
    return errlog.read()


@asynccontextmanager
async def connect(params: StdioServerParameters, errlog):
    """Open an initialised MCP ClientSession over stdio, capturing stderr."""
    async with (
        stdio_client(params, errlog=errlog) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def text_of(result) -> str:
    """Concatenate all text content blocks of a CallToolResult."""
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None) is not None
    )


async def _read_or_reason(impl, url, env, errlog) -> str:
    """Everything the run said: the tool's answer plus the server's stderr.

    A rejected connection surfaces as a failed `initialize` on the Python server
    and as a per-call error on the Node server, so both are folded into one
    string for the negative tests to assert on.
    """
    params = _server_params(impl, url, env)
    text = ""
    try:
        async with connect(params, errlog) as session:
            result = await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
            text = text_of(result)
    except Exception as error:  # the server refused to come up at all
        text = f"{type(error).__name__}: {error}"
    return f"{text}\n{stderr_of(errlog)}"


@pytest.mark.parametrize("mode", ["SignAndEncrypt", "Sign"])
async def test_reads_and_writes_over_a_secured_connection(
    impl, secure_opcua_server, secure_env, errlog, mode
):
    """The whole point: an encrypted, authenticated session that actually works."""
    params = _server_params(impl, secure_opcua_server, {**secure_env, "OPCUA_SECURITY_MODE": mode})
    async with connect(params, errlog) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert tools >= CORE_TOOLS

        assert TEMPERATURE in text_of(
            await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        )

        written = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": TEMPERATURE, "value": "42.5"}]},
        )
        assert '"status": "Good"' in text_of(written), text_of(written)

        read_back = await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        assert "42.5" in text_of(read_back)

    log = stderr_of(errlog)
    # Both runtimes summarise the negotiated security identically on connect.
    assert f'policy=Basic256Sha256 mode={mode} user="{SECURE_USERNAME}"' in log
    # ...and the insecure-connection warning must not fire on a secured one.
    assert "traffic is unencrypted" not in log


async def test_the_password_never_reaches_the_logs(impl, secure_opcua_server, secure_env, errlog):
    """Everything an MCP client shows the user comes from this stream."""
    params = _server_params(impl, secure_opcua_server, secure_env)
    async with connect(params, errlog) as session:
        await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})

    assert SECURE_PASSWORD not in stderr_of(errlog)


async def test_default_mode_is_sign_and_encrypt(impl, secure_opcua_server, secure_env, errlog):
    """A policy with no explicit mode must reach the strongest endpoint, not the weakest."""
    params = _server_params(impl, secure_opcua_server, secure_env)  # no OPCUA_SECURITY_MODE
    async with connect(params, errlog) as session:
        assert TEMPERATURE in text_of(
            await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        )

    assert "mode=SignAndEncrypt" in stderr_of(errlog)


async def test_the_application_uri_comes_from_the_certificate(
    impl, secure_opcua_server, secure_env, errlog
):
    """With no `OPCUA_APPLICATION_URI`, both runtimes announce the certificate's own URI.

    The mock refuses any other URI (`--require-client-uri`), as equipment that
    checks the ApplicationDescription against the `subjectAltName` does, so a
    runtime announcing a library default — python-opcua's `urn:freeopcua:client`
    — cannot get a session here.
    """
    env = {key: value for key, value in secure_env.items() if key != "OPCUA_APPLICATION_URI"}
    params = _server_params(impl, secure_opcua_server, env)
    async with connect(params, errlog) as session:
        assert TEMPERATURE in text_of(
            await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        )


async def test_a_wrong_password_is_rejected(impl, secure_opcua_server, secure_env, errlog):
    """Neither runtime may fall back to a working session when the login fails.

    They surface it at different moments — the Python server activates the
    session in its lifespan and so dies during `initialize`, while the Node
    server connects lazily and reports it per call — but both name the same
    status code.
    """
    reason = await _read_or_reason(
        impl, secure_opcua_server, {**secure_env, "OPCUA_PASSWORD": "wrong"}, errlog
    )
    assert '"status": "Good"' not in reason
    assert "BadUserAccessDenied" in reason


async def test_credentials_without_a_policy_warn_about_clear_text(
    impl, secure_opcua_server, errlog
):
    """A username must not buy silence: it authenticates, it does not encrypt.

    Both client libraries send the password in clear text when the channel is
    `None` and the server's user-token policy specifies no security policy, so
    the warning has to fire before the connection is attempted — which is what
    this asserts, since the connection itself cannot succeed here.
    """
    credentials_only = {"OPCUA_USERNAME": SECURE_USERNAME, "OPCUA_PASSWORD": SECURE_PASSWORD}
    reason = await _read_or_reason(impl, secure_opcua_server, credentials_only, errlog)
    assert "traffic is unencrypted" in reason
    assert "clear text" in reason
    assert SECURE_PASSWORD not in reason


async def test_an_unsecured_client_cannot_use_the_secured_server(impl, secure_opcua_server, errlog):
    """With no security configured there is no endpoint to fall back to."""
    reason = await _read_or_reason(impl, secure_opcua_server, {}, errlog)
    assert '"status": "Good"' not in reason
    # Both runtimes name the policy they could not match.
    assert "SecurityPolicy#None" in reason
    # The default connection is the one that warns.
    assert "traffic is unencrypted" in reason


# --- server-certificate verification (issue #45) --------------------------------


async def test_a_pinned_server_certificate_still_connects(
    impl, secure_opcua_server, secure_env, secure_pki, errlog
):
    """The positive half: pinning the *right* certificate changes nothing visible.

    Worth asserting on its own, because the obvious way to get pinning wrong is
    to make it reject everything — and a negative test alone would pass happily
    against an implementation that could never connect at all.
    """
    env = {**secure_env, "OPCUA_SERVER_CERT": secure_pki["server_cert"]}
    params = _server_params(impl, secure_opcua_server, env)
    async with connect(params, errlog) as session:
        assert TEMPERATURE in text_of(
            await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        )

    log = stderr_of(errlog)
    assert "server-cert=pinned" in log
    # The unverified-server warning is exactly what pinning silences.
    assert "certificate is not being verified" not in log


async def test_a_wrong_server_certificate_is_refused(
    impl, secure_opcua_server, secure_env, tmp_path, errlog
):
    """The test that matters: a server presenting anything else cannot get in.

    A pinning feature that never rejects anything is worse than no pinning at
    all, because it reads as protection. This is the case the whole of #45 is
    for — an attacker on the network path (DNS, ARP, a compromised switch, a
    mistyped endpoint) presenting their own certificate, which both runtimes
    would otherwise encrypt to perfectly happily.

    The impostor certificate is generated here, valid and well-formed, carrying
    the *right* ApplicationUri — so the only thing wrong with it is that it is
    not the server's. Nothing but the pinning can be what rejects it.
    """
    impostor, _ = write_self_signed(tmp_path, "impostor", SECURE_SERVER_URI)
    env = {**secure_env, "OPCUA_SERVER_CERT": str(impostor)}

    reason = await _read_or_reason(impl, secure_opcua_server, env, errlog)

    assert '"status": "Good"' not in reason, (
        f"{impl}: connected to a server whose certificate was not the pinned one: {reason!r}"
    )
    # Deliberately no assertion on *which* status code comes back. Both runtimes
    # fail in the OpenSecureChannel handshake — the client encrypts to the
    # impostor's public key and the real server cannot decrypt it — but they
    # word that very differently, and the Python server's is a rich-formatted
    # traceback whose line wrapping breaks any substring match. The control for
    # "did it fail for the right reason" is
    # `test_a_pinned_server_certificate_still_connects` above: same environment,
    # same server, only the pinned certificate differs, and that one reads a
    # value.


async def test_an_unverified_server_says_so_on_a_secured_connection(
    impl, secure_opcua_server, secure_env, errlog
):
    """Encryption alone is encryption to whoever answered, and now admits it.

    `policy=Basic256Sha256 mode=SignAndEncrypt` reads like the connection is
    safe. The one thing it does not establish is who is on the other end, so the
    absence of pinning is stated rather than left to be inferred.
    """
    params = _server_params(impl, secure_opcua_server, secure_env)  # no OPCUA_SERVER_CERT
    async with connect(params, errlog) as session:
        await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})

    log = stderr_of(errlog)
    assert "certificate is not being verified" in log
    assert "impersonate the endpoint" in log
    assert "server-cert=pinned" not in log


# --- X.509 user authentication (issue #7) ---------------------------------------


async def test_x509_user_authentication_gets_a_working_session(
    impl, secure_opcua_server, secure_pki, tmp_path, errlog
):
    """Both runtimes can authenticate as a *user* with a certificate, not a password.

    The user certificate is deliberately a different key pair from
    `OPCUA_CLIENT_CERT`: that one secures the channel and is the application's
    identity, this one is the user's. Generating a separate pair here is what
    makes the test meaningful — reusing the channel certificate would pass even
    if the implementation ignored `OPCUA_USER_CERT` entirely and fell back to
    the channel identity.

    What this does **not** prove: python-opcua's server validates only
    `UserNameIdentityToken` and accepts an `X509IdentityToken` without checking
    it, so a *wrong* user certificate would get in here too. No mock in this
    repo can show otherwise; see the note in `secure_opcua_server.py`.
    """
    user_cert, user_key = write_self_signed(tmp_path, "user", "urn:opcua-mcp:test-user")
    env = {
        "OPCUA_SECURITY_POLICY": "Basic256Sha256",
        "OPCUA_CLIENT_CERT": secure_pki["client_cert"],
        "OPCUA_CLIENT_KEY": secure_pki["client_key"],
        "OPCUA_APPLICATION_URI": SECURE_CLIENT_URI,
        "OPCUA_SERVER_CERT": secure_pki["server_cert"],
        "OPCUA_USER_CERT": str(user_cert),
        "OPCUA_USER_KEY": str(user_key),
    }
    params = _server_params(impl, secure_opcua_server, env)
    async with connect(params, errlog) as session:
        assert TEMPERATURE in text_of(
            await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        )

    log = stderr_of(errlog)
    assert "user=certificate" in log
    # No password was configured, so none can have been logged.
    assert SECURE_PASSWORD not in log


async def test_a_user_certificate_and_a_username_together_is_a_startup_error(
    impl, secure_opcua_server, secure_env, tmp_path, errlog
):
    """A session carries one user identity; refusing is better than picking one."""
    user_cert, user_key = write_self_signed(tmp_path, "user", "urn:opcua-mcp:test-user")
    env = {
        **secure_env,  # carries OPCUA_USERNAME / OPCUA_PASSWORD
        "OPCUA_USER_CERT": str(user_cert),
        "OPCUA_USER_KEY": str(user_key),
    }

    reason = await _read_or_reason(impl, secure_opcua_server, env, errlog)

    assert "cannot be combined with OPCUA_USERNAME" in reason, f"{impl}: got {reason!r}"
