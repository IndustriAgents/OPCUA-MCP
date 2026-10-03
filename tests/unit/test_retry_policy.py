"""What may follow a request that died on its session (issues #105, #106).

The question this settles is not "is this tool idempotent" — that is
``annotations.idempotentHint``, which is advice to the *model*. It is "may this
server put a second request on the wire when it does not know what happened to
the first", which is ``contract/tools.json`` -> ``retryPolicy``. Both runtimes
used to read the annotation for both, which is how ``write_opcua_nodes`` came to
be automatically re-sent after a lost response.

``packages/server-node/test/retry-policy.test.mjs`` is the Node half and drives
the same shared table through the same wording. Needs no OPC UA server.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.errors import message
from opcua_mcp_server.server import describe_targets

POLICIES = {name for name in CONTRACT["retryPolicies"] if not name.startswith("$")}
TOOLS = CONTRACT["tools"]
TOOL_IDS = [tool["name"] for tool in TOOLS]
CONTROL_CLASSES = {"control", "alarm-action"}

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "uncertain-outcome.json").read_text(encoding="utf-8")
)
CASES = FIXTURE["cases"]
SPECS = {tool["name"]: tool for tool in TOOLS}


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_every_tool_declares_a_known_retry_policy(tool):
    assert tool.get("retryPolicy") in POLICIES, (
        f"{tool['name']} declares retryPolicy {tool.get('retryPolicy')!r}; "
        f"known: {sorted(POLICIES)}"
    )


@pytest.mark.parametrize(
    "tool",
    [t for t in TOOLS if t["accessClass"] in CONTROL_CLASSES],
    ids=[t["name"] for t in TOOLS if t["accessClass"] in CONTROL_CLASSES],
)
def test_no_control_tool_is_ever_re_sent(tool):
    """A control request whose outcome is unknown must not be repeated.

    OPC UA Part 4 §5.11.4 lets a multi-node Write partially succeed, leaves
    rollback to the client and defines no operation order, so a lost response
    never proved the write failed. Re-sending it is a second physical actuation
    on a guess.
    """
    assert tool["retryPolicy"] == "uncertainOutcome"


@pytest.mark.parametrize(
    "tool",
    [t for t in TOOLS if t["annotations"]["readOnlyHint"]],
    ids=[t["name"] for t in TOOLS if t["annotations"]["readOnlyHint"]],
)
def test_reads_are_the_only_thing_re_sent(tool):
    assert tool["retryPolicy"] == "resend"


def test_the_retry_policy_is_not_the_idempotent_hint():
    """The regression this whole field exists for.

    ``write_opcua_nodes`` carries ``idempotentHint: true`` and always should:
    writing 99.9 twice leaves 99.9, which is exactly what that annotation tells
    the model. It is the *transport* that must not read it. If these two ever
    agree again, the derivation has crept back.
    """
    write = SPECS["write_opcua_nodes"]
    assert write["annotations"]["idempotentHint"] is True
    assert write["retryPolicy"] == "uncertainOutcome"


def test_the_retry_policy_is_not_advertised_to_clients():
    """It is this server's business, not the model's. ``tools/list`` must not carry it."""
    for tool in TOOLS:
        assert "retryPolicy" not in tool["annotations"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_uncertain_outcome_names_what_the_call_was_aimed_at(case):
    """The Node half asserts the same sentence for the same call."""
    assert describe_targets(SPECS[case["tool"]], case["arguments"]) == case["targets"]

    rendered = message(
        "uncertainOutcome",
        tool=case["tool"],
        reason=FIXTURE["reason"],
        targets=case["targets"],
    )
    assert case["targets"] in rendered
    assert case["tool"] in rendered


def test_the_uncertain_outcome_never_carries_the_value_being_written():
    """Same rule as the audit trail: targets, never process data."""
    rendered = message(
        "uncertainOutcome",
        tool="write_opcua_nodes",
        reason="BadSessionIdInvalid",
        targets=describe_targets(
            SPECS["write_opcua_nodes"],
            {"nodes": [{"node_id": "ns=2;i=5", "value": 1234.5}]},
        ),
    )
    assert "1234.5" not in rendered


def test_a_guardless_tool_has_nothing_to_name():
    assert describe_targets(SPECS["read_opcua_nodes"], {"node_ids": ["ns=2;i=1"]}) == "unknown"


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("control", [False, True], ids=["read", "write"])
async def test_a_service_timeout_keeps_the_retry_policy(monkeypatch, wrapped, control):
    from types import SimpleNamespace

    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from opcua_mcp_server.policy import ToolPolicy, parse_policy_config
    from opcua_mcp_server.server import create_server
    from opcua_mcp_server.state import ServerState

    policy = ToolPolicy(
        parse_policy_config(
            {
                "OPCUA_PROFILE": "operator",
                "OPCUA_SECURITY_POLICY": "Basic256Sha256",
                "OPCUA_SERVER_CERT": "/pki/server.pem",
                "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=5",
            }
        )
    )
    state = ServerState(policy=policy)
    attempts = []
    rebuilds = []
    connection = SimpleNamespace(
        connecting=False,
        session_id="session-1",
        session_generation=1,
        ensure_connected=lambda: None,
        reconnect=lambda session: rebuilds.append(session),
    )
    state.connection = connection
    mcp = create_server(state)

    async def operation(self, name, arguments, context):
        attempts.append(name)
        if len(attempts) == 1:
            error = TimeoutError()
            if wrapped:
                raise ToolError("Operation failed") from error
            raise error
        return "ok"

    monkeypatch.setattr(MCPServer, "call_tool", operation)
    name = "write_opcua_nodes" if control else "read_opcua_nodes"
    arguments = (
        {"nodes": [{"node_id": "ns=2;i=5", "value": 99.9}]}
        if control
        else {"node_ids": ["ns=2;i=1"]}
    )
    if control:
        with pytest.raises(ToolError, match="whether it took effect is unknown"):
            await mcp.call_tool(name, arguments)
        assert attempts == [name]
    else:
        assert await mcp.call_tool(name, arguments) == "ok"
        assert attempts == [name, name]
    assert rebuilds == ["session-1"]
