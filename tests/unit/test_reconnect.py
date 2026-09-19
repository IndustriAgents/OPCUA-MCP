"""Connection-resilience settings, and what counts as a dead session (issue #18).

Two runtimes recover from the same outage, so they have to agree on *when* to
retry and on *what* is worth retrying. Both questions are settled by pure
functions, which is what lets them be pinned here — and pinned against the Node
server's own implementation rather than against a second copy of the arithmetic.

Needs no OPC UA server, so this runs in the fast CI job.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.config import (
    RECONNECT_DEFAULTS,
    ReconnectConfig,
    describe_reconnect,
    parse_reconnect_config,
    reconnect_budget_ms,
    reconnect_delays,
)
from opcua_mcp_server.connection import (
    DEAD_SESSION_MARKERS,
    DEAD_SESSION_STATUS_CODES,
    is_connection_error,
    not_connected_message,
)

NODE_BUILD = ROOT / "packages" / "server-node" / "build" / "index.js"
NODE_DIR = ROOT / "packages" / "server-node"

#: Settings worth comparing across the runtimes: the defaults, a fast one, retry
#: disabled, and the unlimited case that has no sum to add up.
CASES = [
    ReconnectConfig(),
    ReconnectConfig(initial_delay_ms=250, max_delay_ms=1000, max_retry=8),
    ReconnectConfig(initial_delay_ms=500, max_delay_ms=500, max_retry=0),
    ReconnectConfig(initial_delay_ms=1000, max_delay_ms=10000, max_retry=-1),
]


def _node_eval(expression: str):
    """Evaluate an expression against the built Node server's `config.js`."""
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    script = (
        f"const m = await import('./build/config.js');console.log(JSON.stringify({expression}))"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=NODE_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _as_node(config: ReconnectConfig) -> dict:
    return {
        "initialDelay": config.initial_delay_ms,
        "maxDelay": config.max_delay_ms,
        "maxRetry": config.max_retry,
        "sessionTimeout": config.session_timeout_ms,
    }


# --- defaults ------------------------------------------------------------------


def test_defaults_are_what_an_unset_environment_yields():
    assert parse_reconnect_config({}) == RECONNECT_DEFAULTS


def test_an_empty_value_is_treated_as_unset():
    """An MCP client that writes `"OPCUA_RECONNECT_MAX_RETRY": ""` means "default"."""
    assert parse_reconnect_config({"OPCUA_RECONNECT_MAX_RETRY": "  "}) == RECONNECT_DEFAULTS


def test_the_defaults_retry_but_do_not_hang_a_tool_call():
    """A tool call waits out the whole budget, so the default must stay bearable."""
    assert RECONNECT_DEFAULTS.max_retry > 0, "a dropped connection must be retried by default"
    assert reconnect_budget_ms(RECONNECT_DEFAULTS) <= 15000


# --- parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,value",
    [
        ("OPCUA_RECONNECT_INITIAL_DELAY_MS", "soon"),
        ("OPCUA_RECONNECT_INITIAL_DELAY_MS", "-1"),
        ("OPCUA_RECONNECT_MAX_DELAY_MS", "-5"),
        ("OPCUA_RECONNECT_MAX_RETRY", "many"),
        ("OPCUA_RECONNECT_MAX_RETRY", "-2"),
        ("OPCUA_SESSION_TIMEOUT_MS", "10"),
        ("OPCUA_SESSION_TIMEOUT_MS", "nan"),
    ],
)
def test_a_bad_setting_is_refused(name, value):
    with pytest.raises(ValueError) as error:
        parse_reconnect_config({name: value})
    assert name in str(error.value)
    assert value in str(error.value)


def test_unlimited_retries_are_spelled_minus_one():
    assert parse_reconnect_config({"OPCUA_RECONNECT_MAX_RETRY": "-1"}).max_retry == -1


def test_describe_reconnect_names_every_setting():
    described = describe_reconnect(ReconnectConfig(250, 1000, 8, 30000))
    assert described == "retries=8 backoff=250..1000ms session-timeout=30000ms"
    assert "unlimited" in describe_reconnect(ReconnectConfig(max_retry=-1))


# --- the backoff schedule ------------------------------------------------------


def test_delays_double_up_to_the_ceiling():
    config = ReconnectConfig(initial_delay_ms=250, max_delay_ms=1000, max_retry=5)
    assert reconnect_delays(config) == [250, 500, 1000, 1000, 1000]


def test_no_retries_means_no_delays():
    assert reconnect_delays(ReconnectConfig(max_retry=0)) == []


def test_the_budget_is_the_sum_of_the_delays():
    """The window a tool call waits is exactly the waiting it configured."""
    config = ReconnectConfig(initial_delay_ms=250, max_delay_ms=1000, max_retry=5)
    assert reconnect_budget_ms(config) == sum(reconnect_delays(config))


def test_unlimited_retries_still_yield_a_finite_budget():
    """Waiting forever inside one tool call is never the right answer."""
    budget = reconnect_budget_ms(ReconnectConfig(max_retry=-1, max_delay_ms=10000))
    assert 0 < budget < float("inf")


@pytest.mark.parametrize("case", CASES, ids=[describe_reconnect(c) for c in CASES])
def test_both_runtimes_wait_the_same_amount(case):
    """Pinned against the Node server itself, not against a second copy here."""
    assert reconnect_budget_ms(case) == _node_eval(
        f"m.reconnectBudgetMs({json.dumps(_as_node(case))})"
    )


@pytest.mark.parametrize("case", CASES, ids=[describe_reconnect(c) for c in CASES])
def test_both_runtimes_describe_the_settings_identically(case):
    """The startup log line is what an operator checks a deployment against."""
    assert describe_reconnect(case) == _node_eval(
        f"m.describeReconnect({json.dumps(_as_node(case))})"
    )


def test_both_runtimes_read_the_same_environment_variables():
    env = {
        "OPCUA_RECONNECT_INITIAL_DELAY_MS": "250",
        "OPCUA_RECONNECT_MAX_DELAY_MS": "1000",
        "OPCUA_RECONNECT_MAX_RETRY": "8",
        "OPCUA_SESSION_TIMEOUT_MS": "30000",
    }
    assert _as_node(parse_reconnect_config(env)) == _node_eval(
        f"m.parseReconnectConfig({json.dumps(env)})"
    )


# --- what counts as a dead session ---------------------------------------------


@pytest.mark.parametrize("name", sorted(DEAD_SESSION_STATUS_CODES))
def test_every_status_code_the_contract_names_still_exists_in_the_library(name):
    """The check the old parametrized test could not make.

    That one asserted ``is_connection_error`` against its own constant, so it
    passed by construction and could not detect the failure it existed to catch:
    a client library rewording a message, or dropping a name, and silently
    disabling reconnection. This asserts the contract's names against
    *python-opcua's own enum*, so a name that stops existing there fails here —
    and the numbers come from the library rather than from a transcription, so
    the two runtimes cannot drift apart on what a code means.
    """
    assert DEAD_SESSION_STATUS_CODES[name] == getattr(ua.StatusCodes, name)


def test_a_status_error_is_recognised_by_its_code_not_its_wording():
    """The one check here that a release note cannot break."""
    error = ua.UaStatusCodeError(ua.StatusCodes.BadSessionIdInvalid)
    assert is_connection_error(error)
    # And the wording is genuinely not what is being matched: the same code with
    # its text stripped is still recognised.
    error.args = ()
    assert is_connection_error(error)


def test_a_status_error_for_a_bad_request_is_not_a_dead_session():
    """Retrying a BadNodeIdUnknown on a fresh session would fail identically."""
    assert not is_connection_error(ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown))


@pytest.mark.parametrize("marker", DEAD_SESSION_MARKERS)
def test_every_marker_is_recognised_inside_a_rewrapped_message(marker):
    """By the time an error reaches the dispatcher it is prose, not a status code.

    Still worth having — each tool body re-raises as
    ``ToolError("Failed to read node …: <text>")``, so the text path is the one
    most failures actually take — but it is no longer the *only* check, and it is
    no longer the one relied on to notice library drift.
    """
    assert is_connection_error(RuntimeError(f"Failed to read node ns=2;i=3: {marker}(0x1)"))


@pytest.mark.parametrize(
    "message",
    [
        "Failed to read node ns=2;i=999999: BadNodeIdUnknown",
        'Invalid date/time: "nope". Use ISO 8601, e.g. 2026-04-23T17:40:00Z',
        "Node ns=2;i=3 is not writable under the operator policy",
        "BadOutOfRange",
        "",
    ],
)
def test_a_failed_request_is_not_a_failed_connection(message):
    """Retrying these on a fresh session would fail in exactly the same way."""
    assert not is_connection_error(RuntimeError(message))


def test_a_socket_failure_needs_no_status_code():
    assert is_connection_error(ConnectionRefusedError(61, "Connection refused"))
    assert is_connection_error(TimeoutError())


def test_a_wrapped_cause_is_looked_through():
    """Tool bodies re-raise as `ToolError(...) from e`, so the code is in the cause."""
    cause = ConnectionResetError("reset by peer")
    error = RuntimeError("Failed to write node ns=2;i=3")
    error.__cause__ = cause
    assert is_connection_error(error)


def test_both_runtimes_agree_on_the_markers():
    markers = json.dumps(list(DEAD_SESSION_MARKERS))
    node_markers = _node_eval(
        "await import('./build/connection.js').then(c => "
        f"{markers}.filter(m => c.isConnectionError(new Error('Failed: ' + m))))"
    )
    assert node_markers == list(DEAD_SESSION_MARKERS), (
        "the Node server does not treat every marker the Python server retries on as a dead session"
    )


def test_both_runtimes_resolve_the_same_status_codes():
    """Two libraries, one spec: the numbers must agree, not only the names.

    This is the comparison the old "both runtimes agree on the markers" test
    could not make, because two equal lists of *strings* prove only that the
    transcriptions match — not that either still resolves to the code the
    specification assigns.
    """
    node_codes = _node_eval(
        "await import('./build/connection.js').then(c => c.DEAD_SESSION_STATUS_CODES)"
    )
    assert node_codes == DEAD_SESSION_STATUS_CODES


def test_the_not_connected_message_is_shared_wording():
    message = not_connected_message("opc.tcp://plc:4840", "ECONNREFUSED")
    assert message == (
        "Not connected to the OPC UA server at opc.tcp://plc:4840: ECONNREFUSED. "
        "Call get_server_status for details."
    )
    assert message == _node_eval(
        "await import('./build/connection.js').then(c => "
        "c.notConnectedMessage('opc.tcp://plc:4840', 'ECONNREFUSED'))"
    )
