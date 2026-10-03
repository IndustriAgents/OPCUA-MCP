"""The policy object's newer halves: read guards, URI forms, interlocks, alarm scope.

Each of these is a decision the call path takes on the policy object — whether a
read reaches a hidden node, how the audit names a target, which interlocks guard
a write, whether an alarm is in scope — so each is pinned here against the policy
itself, beside the shared tables that pin the sentences.
"""

from __future__ import annotations

import json

import pytest
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config, values_at
from opcua_mcp_server.server import _audit_targets, describe_targets

SPECS = {tool["name"]: tool for tool in CONTRACT["tools"]}
NAMESPACES = ["http://opcfoundation.org/UA/", "urn:one", "urn:plant"]


def _policy(tmp_path, document: dict, profile: str = "operator", **env: str) -> ToolPolicy:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, **document}), encoding="utf-8")
    config = parse_policy_config(
        {
            "OPCUA_PROFILE": profile,
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_POLICY_FILE": str(path),
            **env,
        }
    )
    policy = ToolPolicy(config)
    policy.bind_namespaces(NAMESPACES)
    return policy


# --- values_at ---------------------------------------------------------------------


def test_a_trailing_brackets_path_is_every_string_element():
    """How readGuard names read_opcua_nodes' node_ids."""
    assert values_at({"node_ids": ["ns=2;i=1", 5, "ns=2;i=2", None]}, "node_ids[]") == [
        "ns=2;i=1",
        "ns=2;i=2",
    ]


def test_a_trailing_brackets_path_on_a_non_list_is_nothing():
    assert values_at({"node_ids": "ns=2;i=1"}, "node_ids[]") == []
    assert values_at({}, "node_ids[]") == []


def test_the_older_forms_still_select_what_they_did():
    arguments = {"nodes": [{"node_id": "ns=2;i=1"}, {"node_id": 3}], "node_id": "ns=2;i=9"}
    assert values_at(arguments, "nodes[].node_id") == ["ns=2;i=1"]
    assert values_at(arguments, "node_id") == ["ns=2;i=9"]


def test_every_read_guard_names_arguments_its_tool_takes():
    for tool in CONTRACT["tools"]:
        for path in (tool.get("readGuard") or {}).get("nodeIdPaths", []):
            assert path.split("[]")[0].split(".")[0] in tool["inputSchema"]["properties"], (
                tool["name"],
                path,
            )


# --- deny_read across profiles ------------------------------------------------------


@pytest.mark.parametrize("profile", ["observe", "operator", "full"])
def test_deny_read_binds_every_profile(tmp_path, profile):
    policy = _policy(tmp_path, {"deny_read": ["ns=2;i=118"]}, profile)
    with pytest.raises(PermissionError, match="not readable under the read policy"):
        policy.authorize("read_opcua_nodes", {"node_ids": ["ns=2;i=118"]})
    with pytest.raises(PermissionError):
        policy.authorize("subscribe_opcua_nodes", {"node_ids": ["ns=2;i=118"]})
    with pytest.raises(PermissionError):
        policy.authorize("read_opcua_history", {"node_id": "nsu=urn:plant;i=118"})


def test_no_deny_read_checks_nothing(tmp_path):
    policy = _policy(tmp_path, {})
    policy.bind_deny([], False, "never")
    policy.authorize_read("read_opcua_nodes", {"node_ids": ["ns=2;i=1"]}, strict=True)


# --- the audit's namespace-URI form ------------------------------------------------


def test_a_node_id_is_also_named_by_its_namespace_uri(tmp_path):
    policy = _policy(tmp_path, {})
    assert policy.uri_form("ns=2;i=41") == "nsu=urn:plant;i=41"
    assert policy.uri_form("i=85") == "nsu=http://opcfoundation.org/UA/;i=85"
    assert policy.uri_form("nsu=urn:one;s=Pump") == "nsu=urn:one;s=Pump"
    assert policy.uri_form("ns=7;i=1") is None
    assert policy.uri_form("nsu=urn:missing;i=1") is None


def test_no_uri_form_before_a_session_has_said_what_its_namespaces_are(tmp_path):
    policy = ToolPolicy(parse_policy_config({}))
    assert policy.uri_form("ns=2;i=41") is None


def test_the_audit_record_carries_the_uri_form_right_after_the_ids(tmp_path):
    policy = _policy(tmp_path, {})
    write = _audit_targets(
        SPECS["write_opcua_nodes"],
        {"nodes": [{"node_id": "ns=2;i=41", "value": 1}, {"node_id": "ns=9;i=1", "value": 2}]},
        policy,
    )
    assert list(write) == ["node_ids", "node_uris"]
    assert write["node_uris"] == ["nsu=urn:plant;i=41", None]
    call = _audit_targets(
        SPECS["call_opcua_method"],
        {"object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=28"},
        policy,
    )
    assert list(call) == ["object_node_id", "method_node_id", "object_node_uri", "method_node_uri"]
    assert call["method_node_uri"] == "nsu=urn:plant;i=28"


def test_the_sentence_a_person_reads_is_unchanged():
    """uncertain-outcome.json pins describe_targets; the URI forms are the audit's alone."""
    assert (
        describe_targets(
            SPECS["write_opcua_nodes"], {"nodes": [{"node_id": "ns=2;i=5", "value": 1}]}
        )
        == "node_ids=ns=2;i=5"
    )


# --- visibility, write_access's policy half ----------------------------------------


def test_a_subtree_rule_alone_offers_write_opcua_nodes(tmp_path):
    policy = _policy(tmp_path, {"control": {"writable_subtrees": [{"root": "ns=2;i=112"}]}})
    assert policy.is_visible(SPECS["write_opcua_nodes"])
    assert policy.write_refusal() is None


def test_write_refusal_is_the_sentence_the_tool_would_be_refused_with(tmp_path):
    policy = _policy(tmp_path, {}, "observe")
    assert policy.write_refusal() == 'Tool "write_opcua_nodes" is disabled by OPCUA_PROFILE=observe'


# --- preconditions -----------------------------------------------------------------


def _interlocked(tmp_path, profile: str = "operator") -> ToolPolicy:
    return _policy(
        tmp_path,
        {
            "control": {
                "writable_nodes": ["ns=2;i=110", "ns=2;i=111"],
                "preconditions": [
                    {
                        "targets": ["ns=2;i=111"],
                        "require": [{"node": "ns=2;i=110", "equals": True}],
                    },
                    {
                        "targets": ["nsu=urn:plant;i=111"],
                        "methods": [{"object_id": "ns=2;i=27", "method_id": "ns=2;i=31"}],
                        "require": [{"node": "ns=2;i=3", "max": 80, "min": None}],
                    },
                ],
            }
        },
        profile,
    )


def test_a_write_collects_every_interlock_on_its_targets(tmp_path):
    guarded = _interlocked(tmp_path).preconditions_for_write(["ns=2;i=110", "ns=2;i=111"])
    assert guarded == [
        (
            "ns=2;i=111",
            [{"node": "ns=2;i=110", "equals": True}, {"node": "ns=2;i=3", "max": 80}],
        )
    ]


def test_a_call_collects_the_interlocks_on_its_pair(tmp_path):
    policy = _interlocked(tmp_path)
    assert policy.preconditions_for_call("ns=2;i=27", "ns=2;i=31") == [
        {"node": "ns=2;i=3", "max": 80}
    ]
    assert policy.preconditions_for_call("ns=2;i=27", "ns=2;i=28") == []


def test_interlocks_are_an_operator_rule(tmp_path):
    policy = _interlocked(tmp_path, "full")
    assert policy.preconditions_for_write(["ns=2;i=111"]) == []
    assert policy.preconditions_for_call("ns=2;i=27", "ns=2;i=31") == []


# --- alarm scope -------------------------------------------------------------------


def test_no_alarm_scope_unless_one_is_configured(tmp_path):
    assert _policy(tmp_path, {"control": {"acknowledge_alarms": True}}).alarm_scope() is None


def test_the_alarm_scope_resolves_its_sources(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "acknowledge_alarms": True,
                "alarm_sources": ["nsu=urn:plant;i=1001", "nsu=urn:missing;i=1"],
                "alarm_max_severity": 500,
            }
        },
    )
    # The unresolvable entry is dropped, which narrows: it allows no source.
    assert policy.alarm_scope() == (["ns=2;i=1001"], 500)


def test_alarm_scope_is_an_operator_rule(tmp_path):
    policy = _policy(tmp_path, {"control": {"alarm_max_severity": 500}}, "full")
    assert policy.alarm_scope() is None


def test_an_interlock_whose_target_does_not_resolve_is_named(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "writable_nodes": ["ns=2;i=111"],
                "preconditions": [
                    {
                        "targets": ["ns=2;i=111"],
                        "require": [{"node": "ns=2;i=110", "equals": True}],
                    },
                    {
                        "methods": [{"object_id": "ns=2;i=27", "method_id": "/Objects/Nowhere"}],
                        "targets": ["nsu=urn:missing;i=1"],
                        "require": [{"node": "ns=2;i=110", "equals": True}],
                    },
                ],
            }
        },
    )
    # Targets before method pairs within one rule, rules in policy order.
    assert policy.unresolved_precondition() == "nsu=urn:missing;i=1"


def test_an_unresolved_method_half_names_the_pair_as_written(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "callable_methods": [{"object_id": "ns=2;i=27", "method_id": "ns=2;i=31"}],
                "preconditions": [
                    {
                        "methods": [{"object_id": "ns=2;i=27", "method_id": "/Objects/Nowhere"}],
                        "require": [{"node": "ns=2;i=110", "equals": True}],
                    }
                ],
            }
        },
    )
    assert policy.unresolved_precondition() == "ns=2;i=27|/Objects/Nowhere"
    policy.bind_paths({"/Objects/Nowhere": "ns=2;i=31"})
    assert policy.unresolved_precondition() is None


def test_unresolved_interlocks_are_an_operator_rule(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "preconditions": [
                    {"targets": ["/Objects/Nowhere"], "require": [{"node": "a", "equals": 1}]}
                ]
            }
        },
        "full",
    )
    assert policy.unresolved_precondition() is None
