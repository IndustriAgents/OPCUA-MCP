"""Neither MCP server may report a deprecation that is not on the allowlist (#150).

pytest's `filterwarnings` stops at the process boundary, and both servers run as
subprocesses here — which is also where the deprecations that matter live. The
one that prompted this, node-opcua's `endpoint_must_exist`, was not a process
warning at all: node-opcua logged it through its own logger on every connect,
so neither `--throw-deprecation` nor any warnings filter would have seen it.
The only place it ever showed up was the server's stderr, so that is what these
tests read.

Each runtime is started with its deprecation reporting turned all the way up —
`PYTHONWARNINGS` for Python, which hides `DeprecationWarning` outside `__main__`
by default, and no `NODE_NO_WARNINGS` for Node — then driven through the paths
where the OPC UA libraries are configured: connect, read, browse, write, and a
secured session authenticated by an X.509 user certificate. Anything on stderr
that says "deprecat…" and is not allowlisted in
`fixtures/deprecation-allowlist.json` fails the test.

Run:
    cd tests && uv run --no-sync pytest e2e/test_deprecations_e2e.py -v
"""

from __future__ import annotations

import tempfile

import pytest
from conftest import SECURE_CLIENT_URI
from deprecations import unexpected_deprecations
from fixtures.pki import write_self_signed
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, records_of, text_of
from test_secure_connection_e2e import TEMPERATURE
from test_secure_connection_e2e import _server_params as _secure_server_params

# `default` rather than `error`: an exception inside the server would surface
# as a failed MCP call with the warning buried in a traceback, where this wants
# the plain warning line, one per call site, to hold against the allowlist.
REPORT_EVERY_DEPRECATION = {
    "PYTHONWARNINGS": "default::DeprecationWarning,default::PendingDeprecationWarning",
}


@pytest.fixture(params=["python", "node"])
def impl(request) -> str:
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip(
            "Node server not built — run `npm install && npm run build` in packages/server-node"
        )
    return request.param


def _reporting(params: StdioServerParameters) -> StdioServerParameters:
    params.env.update(REPORT_EVERY_DEPRECATION)
    # Silences every process warning on Node, deprecations included; a
    # developer's shell exporting it must not make this test pass.
    params.env.pop("NODE_NO_WARNINGS", None)
    return params


async def _drive(params: StdioServerParameters, calls) -> str:
    """Run `calls` against a fresh server and return everything it wrote to stderr."""
    with tempfile.TemporaryFile("w+", errors="replace") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            await calls(session)
        errlog.seek(0)
        return errlog.read()


def _assert_none_unexpected(impl: str, log: str) -> None:
    unexpected = unexpected_deprecations(log, impl)
    assert not unexpected, (
        f"{impl}: the server reported deprecations that are not on the allowlist. "
        "Fix the call site, or — for a warning raised inside a dependency — add an "
        "entry to tests/fixtures/deprecation-allowlist.json with an issue, an "
        "owner and a removal condition:\n  " + "\n  ".join(unexpected)
    )


async def test_an_unsecured_session_reports_no_unexpected_deprecation(impl, opcua_server):
    async def calls(session):
        await session.list_tools()
        read = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["Temperature"]]})
        assert not read.is_error, text_of(read)
        browse = await session.call_tool("browse_opcua_nodes", {"depth": 1})
        assert not browse.is_error, text_of(browse)
        write = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchDouble"], "value": "12.5"}]},
        )
        assert not write.is_error, text_of(write)
        assert records_of(write)[0]["status"] == "Good", text_of(write)

    log = await _drive(_reporting(_server_params(impl, opcua_server)), calls)
    _assert_none_unexpected(impl, log)


async def test_an_x509_user_session_reports_no_unexpected_deprecation(
    impl, secure_opcua_server, secure_pki, tmp_path
):
    """The secured path configures the most library surface: channel security,
    a pinned server certificate, and a user identity signed with a private key —
    the option node-opcua has already deprecated one spelling of (the raw-PEM
    `privateKey`, which `security.ts` avoids by passing `keyOperations`)."""
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

    async def calls(session):
        read = await session.call_tool("read_opcua_nodes", {"node_ids": [TEMPERATURE]})
        assert TEMPERATURE in text_of(read), text_of(read)

    params = _reporting(_secure_server_params(impl, secure_opcua_server, env))
    log = await _drive(params, calls)
    assert "user=certificate" in log, f"{impl}: not an X.509 user session:\n{log[-2000:]}"
    _assert_none_unexpected(impl, log)
