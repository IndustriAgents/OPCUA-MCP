"""Structural invariants of the shared tool contract.

contract/tools.json is the single source of truth both servers derive from, so a
malformed entry breaks both at once. The e2e parity test catches drift *between*
the servers but needs a running mock server and can only compare what the servers
actually advertised; these checks are on the file itself and run in milliseconds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import contract_candidates, load_contract

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text())
TOOLS = CONTRACT["tools"]
CAPABILITIES = CONTRACT["capabilities"]
RESULT_SHAPES = {k: v for k, v in CONTRACT["resultShapes"].items() if not k.startswith("$")}
EVENTS = CONTRACT["events"]
EVENT_FIELDS = EVENTS["fields"]
TOOL_IDS = [t["name"] for t in TOOLS]
EVENT_TOOL_NAMES = {"subscribe_events", "read_events", "list_active_alarms", "acknowledge_alarm"}
SHAPE_IDS = sorted(RESULT_SHAPES)


def test_tool_names_are_unique():
    assert len(TOOL_IDS) == len(set(TOOL_IDS)), "duplicate tool name in the contract"


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_tool_has_required_fields(tool):
    for field in ("name", "capability", "description", "inputSchema"):
        assert field in tool, f"{tool.get('name')} is missing {field!r}"
    assert tool["description"].strip(), f"{tool['name']} has an empty description"


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_capability_is_declared(tool):
    """A tool may only be gated on a capability the contract actually defines."""
    capability = tool["capability"]
    assert capability is None or capability in CAPABILITIES, (
        f"{tool['name']} gated on unknown capability {capability!r}; known: {sorted(CAPABILITIES)}"
    )


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_input_schema_is_coherent(tool):
    schema = tool["inputSchema"]
    assert schema.get("type") == "object", f"{tool['name']} inputSchema must be an object"
    properties = schema.get("properties", {})
    missing = set(schema.get("required", [])) - set(properties)
    assert not missing, f"{tool['name']} requires undeclared properties: {sorted(missing)}"
    for name, spec in properties.items():
        assert spec.get("description", "").strip(), (
            f"{tool['name']}.{name} has no description — the model relies on it"
        )


@pytest.mark.parametrize("name", sorted(CAPABILITIES), ids=sorted(CAPABILITIES))
def test_capability_probe_is_well_formed(name):
    probe = CAPABILITIES[name]
    for field in ("nodeId", "browseName", "check"):
        assert probe.get(field), f"capability {name} is missing {field!r}"
    assert probe["check"] in {"readBooleanTrue", "browseNonEmpty"}, (
        f"capability {name} has unknown check {probe['check']!r}"
    )


def test_python_server_sources_every_description_from_the_contract():
    """The Python server must not carry its own copy of any description."""
    from opcua_mcp_server import DESC

    assert set(DESC) == set(TOOL_IDS)
    for tool in TOOLS:
        assert DESC[tool["name"]] == tool["description"]


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_result_shape_is_declared(tool):
    """A tool may only name a result shape the contract actually defines."""
    shape = tool.get("resultShape")
    assert shape is None or shape in RESULT_SHAPES, (
        f"{tool['name']} declares unknown resultShape {shape!r}; known: {SHAPE_IDS}"
    )


@pytest.mark.parametrize("name", SHAPE_IDS, ids=SHAPE_IDS)
def test_result_shape_is_referenced(name):
    """An unreferenced shape binds nothing and would silently stop being checked."""
    assert any(tool.get("resultShape") == name for tool in TOOLS), (
        f"resultShape {name!r} is defined but no tool declares it"
    )


@pytest.mark.parametrize("name", SHAPE_IDS, ids=SHAPE_IDS)
def test_result_shape_records_are_coherent(name):
    """The record schema must be strict enough for the parity test to enforce it."""
    shape = RESULT_SHAPES[name]
    assert shape["type"] == "array", f"{name} must describe an array of records"
    record = shape["items"]
    assert record["type"] == "object"
    properties = record["properties"]
    assert set(record["required"]) == set(properties), (
        f"{name}: every field must be required, so neither server may omit one"
    )
    assert record.get("additionalProperties") is False, (
        f"{name}: extra fields must be forbidden, or the servers can still diverge"
    )
    for field, spec in properties.items():
        assert spec.get("description", "").strip(), (
            f"{name}.{field} has no description — the model relies on it"
        )


# --- the Alarms & Conditions section -------------------------------------------
# `events` is read by both servers to build one EventFilter and name one record,
# so a mistake here is a mistake in both at once — and one that only shows up
# against a server that actually raises events.


@pytest.mark.parametrize(
    "name",
    [
        "defaultNotifierNodeId",
        "baseEventTypeNodeId",
        "conditionTypeNodeId",
        "conditionRefreshMethodNodeId",
        "acknowledgeMethodNodeId",
        "refreshStartEventTypeNodeId",
        "refreshEndEventTypeNodeId",
    ],
)
def test_event_node_ids_are_written_the_way_the_servers_compare_them(name):
    """Spelled out with their namespace, because that is how both servers render
    a NodeId back — `event_type` is compared against these strings directly."""
    assert EVENTS[name].startswith("ns=0;i="), f"{name} is {EVENTS[name]!r}"


def test_event_fields_are_the_event_record_shape():
    """One list, two jobs: the select clauses and the record's fields.

    If they could differ, a field could be selected and never reported, or
    reported and never selected — and the servers would disagree about which.
    """
    record = RESULT_SHAPES["eventRecords"]["items"]
    assert [field["key"] for field in EVENT_FIELDS] == list(record["properties"]), (
        "contract events.fields and resultShapes.eventRecords must list the same "
        "fields, in the same order"
    )


@pytest.mark.parametrize("field", EVENT_FIELDS, ids=[f["key"] for f in EVENT_FIELDS])
def test_event_field_is_well_formed(field):
    assert set(field) == {"key", "path"}, f"unexpected keys in {field!r}"
    assert field["key"] and field["path"], f"empty entry: {field!r}"
    assert field["key"] == field["key"].lower(), "record fields are snake_case"


def test_event_defaults_cover_every_promised_default():
    defaults = {k: v for k, v in EVENTS["defaults"].items() if not k.startswith("$")}
    assert set(defaults) == {"severityMin", "bufferSize", "readLimit", "refreshTimeoutSeconds"}
    assert all(isinstance(value, int) and value >= 0 for value in defaults.values())


def test_the_event_family_shares_one_result_shape():
    """Four tools, two servers, one shape — the lesson of #23 applied up front."""
    event_family = {t["name"]: t.get("resultShape") for t in TOOLS if t["name"] in EVENT_TOOL_NAMES}
    assert event_family == {
        "subscribe_events": None,
        "read_events": "eventRecords",
        "list_active_alarms": "eventRecords",
        "acknowledge_alarm": None,
    }


def test_the_history_family_shares_one_result_shape():
    """The divergence in #23 was two tools, both servers; one shape covers all four."""
    history_family = {t["name"]: t.get("resultShape") for t in TOOLS if t["capability"]}
    assert history_family == {
        "read_history_opcua_node": "historyRecords",
        "read_aggregate_opcua_node": "historyRecords",
    }


# --- where the Python server looks for the contract ----------------------------
# The contract has to be found from four very different layouts: a checkout, a
# wheel, an sdist-built wheel, and a frozen single-file executable. Getting this
# wrong has already shipped a broken release (a wheel that raised
# FileNotFoundError on import), and it broke again in the frozen build.


def test_the_bundled_copy_is_looked_for_first():
    """The wheel and the frozen app both carry `tools.json` beside this module."""
    candidates = contract_candidates(Path("/site-packages/opcua_mcp_server/contract.py"))
    assert candidates[0] == Path("/site-packages/opcua_mcp_server/tools.json")


def test_a_checkout_also_offers_the_canonical_repo_root_copy():
    module = ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server" / "contract.py"
    assert contract_candidates(module)[1] == ROOT / "contract" / "tools.json"


def test_a_shallow_path_yields_the_bundled_copy_rather_than_raising():
    """Regression guard for the frozen build dying on import.

    PyInstaller unpacks to `/tmp/_MEIabc123/`, which has fewer levels above this
    module than a checkout does. The repo-root fallback used to be computed
    unconditionally while *building* the candidate list, so `Path.parents` raised
    IndexError before the bundled copy could be tried and the executable never
    started. Only on Linux: a macOS unpack directory happens to be deep enough
    that the index is in range, so this passed locally and failed in CI.
    """
    candidates = contract_candidates(Path("/tmp/_MEIabc123/opcua_mcp_server/contract.py"))
    assert candidates == [Path("/tmp/_MEIabc123/opcua_mcp_server/tools.json")]


def test_the_real_contract_is_loadable_from_this_checkout():
    assert load_contract()["tools"]
