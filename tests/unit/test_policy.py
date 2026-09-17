"""Deployment policy is a security boundary, so test it without an OPC UA server."""

from __future__ import annotations

import json

import pytest
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config

TOOLS = {tool["name"]: tool for tool in CONTRACT["tools"]}
CONTROL = {"write_opcua_node", "write_multiple_opcua_nodes", "call_opcua_method"}
ALARM_ACTIONS = {"acknowledge_alarm"}


def policy(env: dict[str, str]) -> ToolPolicy:
    return ToolPolicy(parse_policy_config(env))


def visible(p: ToolPolicy) -> set[str]:
    return {name for name, tool in TOOLS.items() if p.is_visible(tool)}


def test_default_profile_is_observe_and_fails_closed():
    p = policy({})
    assert p.config.profile == "observe"
    assert not (visible(p) & (CONTROL | ALARM_ACTIONS))


def test_read_only_is_an_observe_alias():
    assert visible(policy({"OPCUA_PROFILE": "read-only"})) == visible(policy({}))


def test_full_needs_secure_transport_or_an_explicit_lab_override():
    assert not (visible(policy({"OPCUA_PROFILE": "full"})) & CONTROL)
    p = policy({"OPCUA_PROFILE": "full", "OPCUA_ALLOW_INSECURE_CONTROL": "true"})
    assert visible(p) >= CONTROL | ALARM_ACTIONS


def test_operator_only_exposes_configured_control_targets():
    p = policy(
        {
            "OPCUA_PROFILE": "operator",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13",
            "OPCUA_ALLOWED_METHODS": "ns=2;i=27|ns=2;i=28",
            "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS": "true",
        }
    )
    assert visible(p) >= CONTROL | ALARM_ACTIONS
    p.authorize("write_opcua_node", {"node_id": "ns=2;i=13", "value": "1"})
    p.authorize(
        "call_opcua_method",
        {"object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=28"},
    )
    with pytest.raises(PermissionError, match="not writable"):
        p.authorize("write_opcua_node", {"node_id": "ns=2;i=14", "value": "1"})
    with pytest.raises(PermissionError, match="not allowed"):
        p.authorize(
            "call_opcua_method",
            {"object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=29"},
        )


def test_operator_catalog_hides_control_families_without_targets():
    base = {"OPCUA_PROFILE": "operator", "OPCUA_SECURITY_POLICY": "Basic256Sha256"}
    write_only = policy({**base, "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13"})
    assert visible(write_only) & CONTROL == {
        "write_opcua_node",
        "write_multiple_opcua_nodes",
    }
    method_only = policy({**base, "OPCUA_ALLOWED_METHODS": "ns=2;i=27|ns=2;i=28"})
    assert visible(method_only) & CONTROL == {"call_opcua_method"}


def test_batch_write_is_rejected_before_any_item_can_run():
    p = policy(
        {
            "OPCUA_PROFILE": "operator",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13",
        }
    )
    with pytest.raises(PermissionError, match="ns=2;i=14"):
        p.authorize(
            "write_multiple_opcua_nodes",
            {
                "nodes_to_write": [
                    {"node_id": "ns=2;i=13", "value": "1"},
                    {"node_id": "ns=2;i=14", "value": "2"},
                ]
            },
        )


def test_allowed_tools_can_only_narrow_a_profile():
    p = policy(
        {
            "OPCUA_PROFILE": "full",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_ALLOWED_TOOLS": "read_opcua_node,write_opcua_node",
        }
    )
    assert visible(p) == {"read_opcua_node", "write_opcua_node"}


def test_unknown_profile_and_tool_fail_at_configuration_time():
    with pytest.raises(ValueError, match="Invalid OPCUA_PROFILE"):
        parse_policy_config({"OPCUA_PROFILE": "god-mode"})
    with pytest.raises(ValueError, match="Unknown tool"):
        parse_policy_config({"OPCUA_ALLOWED_TOOLS": "not_a_tool"})


def test_json_policy_file_and_environment_precedence(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profile": "operator",
                "control": {
                    "writable_nodes": ["ns=2;i=13"],
                    "acknowledge_alarms": True,
                },
            }
        )
    )
    parsed = parse_policy_config(
        {
            "OPCUA_POLICY_FILE": str(path),
            "OPCUA_PROFILE": "observe",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
        }
    )
    assert parsed.profile == "observe"
    assert parsed.writable_nodes == {"ns=2;i=13"}


@pytest.mark.parametrize("raw", ["maybe", "enabled", "2"])
def test_boolean_configuration_is_strict(raw):
    with pytest.raises(ValueError, match="must be true or false"):
        parse_policy_config({"OPCUA_ALLOW_INSECURE_CONTROL": raw})
