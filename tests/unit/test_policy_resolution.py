"""The policy resolved against a server: browse paths, writable_subtrees, deny_read.

``policy-check.json`` pins what the check *says* once everything is resolved.
This is the resolving: that a browse path matching two children resolves to
nothing, that a subtree rule matches by each node's own HasTypeDefinition and
contributes nothing past its cap, that deny_read covers a whole subtree and
refuses every guarded read when it cannot be resolved in full — and that each of
those is bound into the policy the call path authorizes against.
"""

from __future__ import annotations

import json

import pytest
from fake_address_space import FakeAddressSpace
from opcua import ua
from opcua_mcp_server import policy_resolution
from opcua_mcp_server.node_facts import NodeFacts
from opcua_mcp_server.node_metadata import NodeMetadata
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config
from opcua_mcp_server.policy_resolution import check_policy, resolve_path, walk

ICS = "ns=2;i=1"
LINE1 = "ns=2;i=112"
RECIPES = "ns=2;i=118"
ANALOG_ITEM_TYPE = "ns=0;i=2368"


def _plant() -> FakeAddressSpace:
    """A slice of the mock: a line of typed tags, a hidden folder, a duplicated name."""
    space = FakeAddressSpace()
    space.add(ICS, (2, "IndustrialControlSystem"), ua.NodeClass.Object, parent="ns=0;i=85")
    space.add(LINE1, (2, "Line1"), ua.NodeClass.Object, parent=ICS)
    space.variable("ns=2;i=113", "LineSpeed", LINE1, type_definition=ANALOG_ITEM_TYPE)
    space.variable("ns=2;i=115", "LineTension", LINE1, type_definition=ANALOG_ITEM_TYPE)
    space.variable("ns=2;i=117", "LineLabel", LINE1, data_type="ns=0;i=12", value="L1")
    space.property("ns=2;i=113", "ns=2;i=114", "EURange", None)
    space.add(RECIPES, (2, "Recipes"), ua.NodeClass.Object, parent=ICS)
    space.variable("ns=2;i=119", "SecretRecipe", RECIPES, data_type="ns=0;i=12", value="x")
    # Two children called Pump, in two namespaces.
    space.add("ns=2;i=7", (2, "Plant"), ua.NodeClass.Object, parent="ns=0;i=85")
    space.add("ns=2;i=8", (2, "Pump"), ua.NodeClass.Object, parent="ns=2;i=7")
    space.add("ns=3;i=8", (3, "Pump"), ua.NodeClass.Object, parent="ns=2;i=7")
    return space


def _policy(tmp_path, document: dict, profile: str = "operator") -> ToolPolicy:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, **document}), encoding="utf-8")
    config = parse_policy_config(
        {
            "OPCUA_PROFILE": profile,
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_POLICY_FILE": str(path),
        }
    )
    policy = ToolPolicy(config)
    policy.bind_namespaces(["http://opcfoundation.org/UA/", "urn:one", "urn:plant", "urn:three"])
    return policy


def _check(space: FakeAddressSpace, policy: ToolPolicy) -> dict:
    return check_policy(space.client(), policy, NodeFacts(), NodeMetadata(), {}, 3)


# --- browse paths -------------------------------------------------------------------


def test_a_path_resolves_to_the_one_node_it_names():
    found = resolve_path(_plant().client(), "/Objects/IndustrialControlSystem/Line1/LineSpeed")
    assert found.node_id == "ns=2;i=113"


def test_a_segment_matching_two_children_resolves_to_nothing():
    """A browse may pick the first match; an allowlist that did would authorize a guess."""
    found = resolve_path(_plant().client(), "/Objects/Plant/Pump")
    assert (found.node_id, found.reason, found.segment, found.parent) == (
        None,
        "pathAmbiguous",
        "Pump",
        "ns=2;i=7",
    )


def test_a_qualified_segment_settles_the_ambiguity():
    assert resolve_path(_plant().client(), "/Objects/Plant/3:Pump").node_id == "ns=3;i=8"


def test_a_missing_segment_is_unresolved():
    found = resolve_path(_plant().client(), "/Objects/Nowhere")
    assert (found.node_id, found.reason, found.segment) == (None, "unresolved", "Nowhere")


def test_a_browse_that_fails_is_an_error_not_a_missing_node():
    space = _plant()
    space.browse_status[ICS] = "BadUserAccessDenied"
    with pytest.raises(ValueError, match="BadUserAccessDenied"):
        resolve_path(space.client(), "/Objects/IndustrialControlSystem/Line1")


def test_a_path_entry_matches_nothing_before_it_is_resolved(tmp_path):
    path = "/Objects/IndustrialControlSystem/Line1/LineSpeed"
    policy = _policy(tmp_path, {"control": {"writable_nodes": [path]}})
    assert not policy.is_allowlisted("ns=2;i=113")
    _check(_plant(), policy)
    assert policy.is_allowlisted("ns=2;i=113")


def test_an_ambiguous_allowlist_path_allows_nothing_and_says_why(tmp_path):
    policy = _policy(tmp_path, {"control": {"writable_nodes": ["/Objects/Plant/Pump"]}})
    report = _check(_plant(), policy)
    assert not policy.is_allowlisted("ns=2;i=8") and not policy.is_allowlisted("ns=3;i=8")
    assert report["findings"] == [
        {
            "entry": "/Objects/Plant/Pump",
            "problem": (
                '/Objects/Plant/Pump is ambiguous: more than one child of ns=2;i=7 matches "Pump", '
                "so it allows or hides nothing. Qualify the segment with its namespace index, "
                "or use a node ID."
            ),
        }
    ]


def test_a_method_pair_may_be_written_as_paths(tmp_path):
    space = _plant()
    space.add(
        "ns=2;i=31",
        (2, "Stop"),
        ua.NodeClass.Method,
        parent=ICS,
        reference=ua.ObjectIds.HasComponent,
        executable=True,
        user_executable=True,
    )
    policy = _policy(
        tmp_path,
        {
            "control": {
                "callable_methods": [
                    {
                        "object_id": "/Objects/IndustrialControlSystem",
                        "method_id": "/Objects/IndustrialControlSystem/Stop",
                    }
                ]
            }
        },
    )
    report = _check(space, policy)
    assert report["callable_methods"] == ["ns=2;i=1|ns=2;i=31"]
    policy.authorize("call_opcua_method", {"object_node_id": ICS, "method_node_id": "ns=2;i=31"})


# --- the walk ---------------------------------------------------------------------


def test_a_typed_walk_matches_each_nodes_own_type_definition():
    walked = walk(
        _plant().client(),
        LINE1,
        {},
        keep=lambda reference: reference.NodeClass == ua.NodeClass.Variable,
        limit=10,
        strict=False,
        type_definition=ANALOG_ITEM_TYPE,
    )
    assert walked == (["ns=2;i=113", "ns=2;i=115"], False)


def test_the_parents_copy_of_a_type_is_not_trusted():
    """python-opcua's server lists a retyped child under its old type."""
    space = _plant()
    space.nodes["ns=2;i=113"].type_definition = "ns=0;i=63"  # what the parent says
    walked = walk(
        space.client(),
        LINE1,
        {},
        keep=lambda reference: reference.NodeClass == ua.NodeClass.Variable,
        limit=10,
        strict=False,
        type_definition=ANALOG_ITEM_TYPE,
    )
    assert walked == (["ns=2;i=113", "ns=2;i=115"], False)


def test_an_untyped_walk_takes_every_variable_properties_included():
    walked = walk(
        _plant().client(),
        LINE1,
        {},
        keep=lambda reference: reference.NodeClass == ua.NodeClass.Variable,
        limit=10,
        strict=False,
    )
    assert walked is not None
    assert sorted(walked[0]) == ["ns=2;i=113", "ns=2;i=114", "ns=2;i=115", "ns=2;i=117"]


def test_a_walk_past_its_limit_says_so_and_stops():
    walked = walk(
        _plant().client(), "ns=0;i=85", {}, keep=lambda reference: True, limit=2, strict=True
    )
    assert walked is not None and walked[1] is True


def test_a_walk_from_an_unknown_root_names_nothing():
    assert (
        walk(_plant().client(), "ns=2;i=999", {}, keep=lambda r: True, limit=5, strict=True) is None
    )


def test_a_strict_walk_fails_on_a_node_below_the_root_it_cannot_list():
    space = _plant()
    space.browse_status[RECIPES] = "BadTimeout"
    with pytest.raises(policy_resolution.WalkFailed, match="BadTimeout"):
        walk(space.client(), ICS, {}, keep=lambda reference: True, limit=50, strict=True)


def test_a_lenient_walk_skips_a_node_below_the_root_it_cannot_list():
    space = _plant()
    space.browse_status[RECIPES] = "BadTimeout"
    walked = walk(space.client(), ICS, {}, keep=lambda reference: True, limit=50, strict=False)
    assert walked is not None and "ns=2;i=113" in walked[0]


def test_a_walk_browses_a_level_at_a_time():
    space = _plant()
    walk(space.client(), ICS, {}, keep=lambda reference: True, limit=50, strict=True)
    # ICS; then Line1 and Recipes together; then everything under them.
    assert space.browse_sizes[:2] == [1, 2]


# --- writable_subtrees ------------------------------------------------------------


def test_a_subtree_rule_allows_its_typed_variables_with_its_bound(tmp_path):
    rule = {"root": "/Objects/IndustrialControlSystem/Line1", "type_definition": "i=2368"}
    policy = _policy(tmp_path, {"control": {"writable_subtrees": [{**rule, "max": 40}]}})
    report = _check(_plant(), policy)
    assert report["writable_nodes"] == ["ns=2;i=113", "ns=2;i=115"]
    assert report["findings"] == []
    assert policy.is_allowlisted("ns=2;i=113") and not policy.is_allowlisted("ns=2;i=117")
    assert policy.bound_for("ns=2;i=115").maximum == 40
    with pytest.raises(PermissionError, match="set by the operator policy"):
        policy.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=113", "value": 45}]})


def test_an_explicit_entry_bounds_a_node_as_it_names_it(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "writable_nodes": ["ns=2;i=113"],
                "writable_subtrees": [{"root": LINE1, "max": 40}],
            }
        },
    )
    _check(_plant(), policy)
    assert policy.bound_for("ns=2;i=113") is None
    assert policy.bound_for("ns=2;i=115").maximum == 40


def test_the_first_rule_to_match_a_node_bounds_it(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "writable_subtrees": [
                    {"root": LINE1, "type_definition": "i=2368", "max": 10},
                    {"root": ICS, "max": 99},
                ]
            }
        },
    )
    _check(_plant(), policy)
    assert policy.bound_for("ns=2;i=113").maximum == 10
    assert policy.bound_for("ns=2;i=117").maximum == 99


def test_a_rule_that_matches_too_much_allows_nothing(tmp_path):
    policy = _policy(tmp_path, {"control": {"writable_subtrees": [{"root": ICS, "max_nodes": 2}]}})
    report = _check(_plant(), policy)
    assert report["writable_nodes"] == []
    assert report["findings"][0]["problem"].startswith(
        "writable_subtrees rule ns=2;i=1 matches more than 2 nodes"
    )


def test_a_rule_that_matches_nothing_and_one_whose_root_is_missing(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "writable_subtrees": [
                    {"root": RECIPES, "type_definition": "i=2368"},
                    {"root": "/Objects/Nowhere"},
                ]
            }
        },
    )
    report = _check(_plant(), policy)
    assert [finding["entry"] for finding in report["findings"]] == [RECIPES, "/Objects/Nowhere"]
    assert "matched no Variables" in report["findings"][0]["problem"]
    assert "does not resolve" in report["findings"][1]["problem"]


# --- deny_read ---------------------------------------------------------------------


def test_deny_read_hides_an_entry_and_everything_under_it(tmp_path):
    policy = _policy(
        tmp_path, {"deny_read": ["/Objects/IndustrialControlSystem/Recipes"]}, "observe"
    )
    report = _check(_plant(), policy)
    assert (report["read_denied"], report["read_policy_complete"]) == (2, True)
    assert report["writable_nodes"] == [] and report["callable_methods"] == []
    with pytest.raises(PermissionError, match="Node ns=2;i=119 is not readable"):
        policy.authorize("read_opcua_nodes", {"node_ids": ["ns=2;i=41", "ns=2;i=119"]})
    with pytest.raises(PermissionError, match=RECIPES):
        policy.authorize("browse_opcua_nodes", {"node_id": RECIPES})
    policy.authorize("read_opcua_nodes", {"node_ids": ["ns=2;i=113"]})


def test_a_plain_deny_entry_hides_its_node_before_any_session(tmp_path):
    policy = _policy(tmp_path, {"deny_read": [RECIPES]}, "observe")
    assert policy.read_denied(RECIPES)
    assert not policy.read_denied("ns=2;i=119")
    _check(_plant(), policy)
    assert policy.read_denied("ns=2;i=119")


def test_an_unresolved_deny_entry_hides_nothing_and_is_reported(tmp_path):
    policy = _policy(tmp_path, {"deny_read": ["/Objects/Nowhere", "ns=2;i=999"]}, "observe")
    report = _check(_plant(), policy)
    assert report["read_policy_complete"] is True
    assert [finding["entry"] for finding in report["findings"]] == [
        "/Objects/Nowhere",
        "ns=2;i=999",
    ]


def test_a_deny_set_past_the_cap_refuses_every_guarded_read(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_resolution, "MAX_DENY_NODES", 3)
    policy = _policy(tmp_path, {"deny_read": [RECIPES, ICS]}, "observe")
    report = _check(_plant(), policy)
    assert report["read_policy_complete"] is False
    assert report["findings"] == [
        {
            "entry": ICS,
            "problem": (
                "deny_read entry ns=2;i=1 takes the read policy past 3 nodes, so every read is "
                "refused until deny_read is narrowed."
            ),
        }
    ]
    with pytest.raises(PermissionError) as refused:
        policy.authorize_read("read_opcua_nodes", {"node_ids": ["ns=2;i=41"]}, strict=True)
    # The finding without its full stop: the template carries on after {reason}.
    assert str(refused.value) == (
        "Reads are refused until the read policy (deny_read) is fully resolved: deny_read entry "
        "ns=2;i=1 takes the read policy past 3 nodes, so every read is refused until deny_read "
        "is narrowed. get_server_status reports it under policy_check."
    )
    # Before the session is up the check is not strict; the strict one follows.
    policy.authorize_read("read_opcua_nodes", {"node_ids": ["ns=2;i=41"]}, strict=False)


def test_a_deny_walk_that_fails_refuses_every_guarded_read(tmp_path):
    space = _plant()
    space.browse_status["ns=2;i=119"] = "BadTimeout"
    policy = _policy(tmp_path, {"deny_read": [RECIPES]}, "observe")
    report = _check(space, policy)
    assert report["read_policy_complete"] is False
    assert report["findings"][0]["problem"] == (
        "deny_read entry ns=2;i=118 could not be resolved (BadTimeout), so every read is "
        "refused until it is."
    )
    with pytest.raises(PermissionError, match="fully resolved"):
        policy.authorize_read("read_opcua_history", {"node_id": "ns=2;i=41"}, strict=True)


def test_a_read_tool_without_a_read_guard_is_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_resolution, "MAX_DENY_NODES", 1)
    policy = _policy(tmp_path, {"deny_read": [ICS]}, "observe")
    _check(_plant(), policy)
    policy.authorize_read("list_active_alarms", {}, strict=True)


# --- the report --------------------------------------------------------------------


def test_the_report_names_what_cannot_work_and_is_printed(tmp_path, capsys):
    space = _plant()
    space.variable("ns=2;i=3", "Temperature", ICS, access_level=5, user_access_level=5)
    policy = _policy(
        tmp_path,
        {"control": {"writable_nodes": ["ns=2;i=3", ICS, "nsu=urn:missing;i=1"]}},
    )
    report = _check(space, policy)
    assert report["generation"] == 3
    assert report["writable_nodes"] == sorted(["ns=2;i=3", ICS])
    assert [finding["entry"] for finding in report["findings"]] == [
        "ns=2;i=3",
        ICS,
        "nsu=urn:missing;i=1",
    ]
    stderr = capsys.readouterr().err
    for finding in report["findings"]:
        assert f"WARNING: policy check: {finding['problem']}" in stderr


def test_observe_resolves_no_control_rule(tmp_path):
    space = _plant()
    policy = _policy(
        tmp_path,
        {"control": {"writable_nodes": ["/Objects/IndustrialControlSystem/Line1/LineSpeed"]}},
        "observe",
    )
    report = _check(space, policy)
    assert report["findings"] == [] and report["writable_nodes"] == []
    assert space.browse_sizes == []


def test_a_check_that_breaks_never_raises_and_holds_reads_back(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(policy_resolution, "_expand_deny", broken)
    policy = _policy(tmp_path, {"deny_read": [RECIPES]}, "observe")
    report = _check(_plant(), policy)
    assert report["read_policy_complete"] is False
    with pytest.raises(PermissionError):
        policy.authorize_read("read_opcua_nodes", {"node_ids": ["ns=2;i=41"]}, strict=True)


def test_the_report_lints_interlock_targets_pairs_then_requirements(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "control": {
                "writable_nodes": ["ns=2;i=113"],
                "preconditions": [
                    {
                        "targets": ["/Objects/Nowhere", "ns=2;i=113"],
                        "methods": [{"object_id": ICS, "method_id": "/Objects/Gone"}],
                        "require": [{"node": "/Objects/Permit", "equals": True}],
                    }
                ],
            }
        },
    )
    report = _check(_plant(), policy)
    assert [finding["entry"] for finding in report["findings"]] == [
        "/Objects/Nowhere",
        "ns=2;i=1|/Objects/Gone",
        "/Objects/Permit",
    ]
