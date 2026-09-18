"""Deployment policy is a security boundary, so test it without an OPC UA server."""

from __future__ import annotations

import json

import pytest
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.policy import ToolPolicy, describe_policy, parse_policy_config

TOOLS = {tool["name"]: tool for tool in CONTRACT["tools"]}
CONTROL = {"write_opcua_nodes", "call_opcua_method"}
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
    p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "1"}]})
    p.authorize(
        "call_opcua_method",
        {"object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=28"},
    )
    with pytest.raises(PermissionError, match="not writable"):
        p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=14", "value": "1"}]})
    with pytest.raises(PermissionError, match="not allowed"):
        p.authorize(
            "call_opcua_method",
            {"object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=29"},
        )


def test_operator_catalog_hides_control_families_without_targets():
    base = {"OPCUA_PROFILE": "operator", "OPCUA_SECURITY_POLICY": "Basic256Sha256"}
    write_only = policy({**base, "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13"})
    assert visible(write_only) & CONTROL == {
        "write_opcua_nodes",
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
            "write_opcua_nodes",
            {
                "nodes": [
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
            "OPCUA_ALLOWED_TOOLS": "read_opcua_nodes,write_opcua_nodes",
        }
    )
    assert visible(p) == {"read_opcua_nodes", "write_opcua_nodes"}


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


# --- contract-derived guards (the safety layer stops keying off tool names) ------


def operator(**extra: str) -> ToolPolicy:
    """An operator profile on a secured channel, with one writable node and one method."""
    return policy(
        {
            "OPCUA_PROFILE": "operator",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13",
            "OPCUA_ALLOWED_METHODS": "ns=2;i=1|ns=2;i=2",
            **extra,
        }
    )


def test_every_control_tool_in_the_contract_declares_a_guard():
    """The check that keeps this architecture honest as tools are added.

    A `control` or `alarm-action` tool with no `guard` is denied at runtime, so
    forgetting one is safe — but it is also invisible, and the author would find
    out by way of a tool that mysteriously does nothing. Failing here instead
    says so at the moment the contract is edited.
    """
    for tool in CONTRACT["tools"]:
        if tool["accessClass"] in {"control", "alarm-action"}:
            guard = tool.get("guard")
            assert guard, f"{tool['name']} declares no guard"
            assert guard.keys() & {"nodeIdPaths", "methodPaths", "flag"}, (
                f"{tool['name']} has a guard that authorises nothing"
            )


def test_a_control_tool_without_a_guard_is_denied_not_allowed():
    """The fail-open this replaced: an unguarded control tool used to be callable.

    `_class_visible` ended in `return bool(writable_nodes)`, so *any* control
    tool the policy did not recognise by name became visible as soon as one node
    was writable — and `authorize`'s if/else chain then checked nothing at all.
    A tool added to the contract was a tool with no argument validation.
    """
    invented = {
        "name": "reboot_the_plc",
        "accessClass": "control",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    }
    p = operator()
    assert not p._class_visible(invented)
    # ...and under `full`, which skips allowlists, it is still refused: "no
    # allowlists" must not also mean "no idea what this is, so yes".
    assert not policy(
        {"OPCUA_PROFILE": "full", "OPCUA_ALLOW_INSECURE_CONTROL": "true"}
    )._class_visible(invented)


def test_an_unknown_access_class_is_denied():
    """A contract is data, so its accessClass can carry a typo the compiler never sees."""
    typo = {
        "name": "write_something",
        "accessClass": "controll",
        "guard": {"nodeIdPaths": ["node_id"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    }
    assert not operator()._class_visible(typo)
    assert not policy(
        {"OPCUA_PROFILE": "full", "OPCUA_ALLOW_INSECURE_CONTROL": "true"}
    )._class_visible(typo)


def test_a_guard_path_that_selects_nothing_denies():
    """A write whose target cannot be located is a write whose target cannot be checked."""
    p = operator()
    with pytest.raises(PermissionError, match=r"requires nodes\.node_id"):
        p.authorize("write_opcua_nodes", {"nodes": [{"value": 1}]})
    with pytest.raises(PermissionError, match=r"requires nodes\.node_id"):
        p.authorize("write_opcua_nodes", {})


def test_one_forbidden_target_rejects_the_whole_batch():
    """A batch must never end up partially authorised."""
    p = operator()
    with pytest.raises(PermissionError, match="ns=2;i=99 is not writable"):
        p.authorize(
            "write_opcua_nodes",
            {"nodes": [{"node_id": "ns=2;i=13"}, {"node_id": "ns=2;i=99"}]},
        )


# --- node-id canonicalisation in the allowlist ----------------------------------


@pytest.mark.parametrize(
    ("allowed", "requested"),
    [
        ("i=2253", "ns=0;i=2253"),
        ("ns=0;i=2253", "i=2253"),
        ("ns=2;i=13", " ns=2;i=13 "),
        (" ns=2;i=13 ", "ns=2;i=13"),
    ],
)
def test_the_allowlist_matches_the_same_node_spelled_either_way(allowed, requested):
    """`i=2253` and `ns=0;i=2253` are the same node; raw string equality says otherwise.

    The old matching was `node_id not in writable_nodes` on untrimmed strings, so
    an entry written one way silently never matched a request written the other
    — a denial, which is safe, but indistinguishable from a policy mistake.
    """
    p = operator(OPCUA_ALLOWED_WRITE_NODES=allowed)
    p.authorize("write_opcua_nodes", {"nodes": [{"node_id": requested, "value": 1}]})


def test_a_method_pair_is_canonicalised_on_both_sides():
    p = operator(OPCUA_ALLOWED_METHODS="i=1|i=2")
    p.authorize("call_opcua_method", {"object_node_id": "ns=0;i=1", "method_node_id": "ns=0;i=2"})


def test_the_same_method_under_a_different_object_is_a_different_operation():
    p = operator()
    with pytest.raises(PermissionError, match="not allowed by the operator policy"):
        p.authorize(
            "call_opcua_method", {"object_node_id": "ns=2;i=9", "method_node_id": "ns=2;i=2"}
        )


# --- namespace-URI allowlists ---------------------------------------------------

NAMESPACES = ["http://opcfoundation.org/UA/", "urn:plant:line-a"]


def test_a_namespace_uri_entry_authorises_the_node_at_that_uris_index():
    """The point of the `nsu=` form: the URI is stable, the index is not."""
    p = operator(OPCUA_ALLOWED_WRITE_NODES="nsu=urn:plant:line-a;i=5")
    p.bind_namespaces(NAMESPACES)
    p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=1;i=5", "value": 1}]})


def test_the_same_entry_follows_a_reordered_namespace_array():
    """A firmware update that reorders namespaces moves the index; the URI does not.

    This is the failure the whole form exists to prevent: written `ns=1;i=5`, the
    allowlist would go on authorising index 1 after the reorder — which is now a
    different physical node, with nothing reporting anything wrong.
    """
    p = operator(OPCUA_ALLOWED_WRITE_NODES="nsu=urn:plant:line-a;i=5")
    p.bind_namespaces(NAMESPACES)
    p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=1;i=5", "value": 1}]})

    p.bind_namespaces(["http://opcfoundation.org/UA/", "urn:other", "urn:plant:line-a"])
    p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=5", "value": 1}]})
    with pytest.raises(PermissionError, match="not writable"):
        p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=1;i=5", "value": 1}]})


def test_a_namespace_uri_the_server_does_not_publish_authorises_nothing():
    p = operator(OPCUA_ALLOWED_WRITE_NODES="nsu=urn:not:here;i=5")
    p.bind_namespaces(NAMESPACES)
    for candidate in ("ns=0;i=5", "ns=1;i=5", "nsu=urn:not:here;i=5"):
        with pytest.raises(PermissionError, match="not writable"):
            p.authorize("write_opcua_nodes", {"nodes": [{"node_id": candidate, "value": 1}]})


def test_a_namespace_uri_entry_denies_until_the_namespaces_are_known():
    """Unknown is not empty. Resolving optimistically would authorise the wrong node."""
    p = operator(OPCUA_ALLOWED_WRITE_NODES="nsu=urn:plant:line-a;i=5")
    with pytest.raises(PermissionError, match="not writable"):
        p.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=1;i=5", "value": 1}]})


def test_binding_namespaces_warns_about_an_entry_that_can_never_match(capsys):
    """Loud, because the failure it prevents is silent."""
    p = operator(OPCUA_ALLOWED_WRITE_NODES="nsu=urn:not:here;i=5")
    p.bind_namespaces(NAMESPACES)
    assert "does not publish" in capsys.readouterr().err


# --- the startup summary --------------------------------------------------------


def test_the_startup_summary_distinguishes_a_lab_override_from_a_secured_channel():
    """It used to print `insecure-control=enabled` for both, which is the opposite
    of conspicuous: the one line an operator might scan said the same thing
    whether control was properly secured or deliberately unlocked."""
    secured = describe_policy(policy({"OPCUA_SECURITY_POLICY": "Basic256Sha256"}))
    override = describe_policy(policy({"OPCUA_ALLOW_INSECURE_CONTROL": "true"}))
    blocked = describe_policy(policy({}))

    assert "control=secured" in secured
    assert "control=INSECURE-OVERRIDE" in override
    assert "control=blocked" in blocked
    assert len({secured, override, blocked}) == 3
