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
RESOURCES = CONTRACT["resources"]
RESULT_SHAPES = {k: v for k, v in CONTRACT["resultShapes"].items() if not k.startswith("$")}
EVENTS = CONTRACT["events"]
EVENT_FIELDS = EVENTS["fields"]
TOOL_IDS = [t["name"] for t in TOOLS]
EVENT_TOOL_NAMES = {"subscribe_events", "read_events", "list_active_alarms", "acknowledge_alarm"}
RESOURCE_IDS = [r["uri"] for r in RESOURCES]
SHAPE_IDS = sorted(RESULT_SHAPES)


def test_tool_names_are_unique():
    assert len(TOOL_IDS) == len(set(TOOL_IDS)), "duplicate tool name in the contract"


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_tool_has_required_fields(tool):
    for field in (
        "name",
        "accessClass",
        "annotations",
        "capabilities",
        "description",
        "inputSchema",
    ):
        assert field in tool, f"{tool.get('name')} is missing {field!r}"
    assert tool["description"].strip(), f"{tool['name']} has an empty description"


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_tool_access_metadata_is_safe(tool):
    access = tool["accessClass"]
    assert access in {"read", "monitor", "alarm-action", "control"}
    annotations = tool["annotations"]
    assert set(annotations) == {"readOnlyHint", "destructiveHint", "idempotentHint"}
    assert all(isinstance(value, bool) for value in annotations.values())
    if access in {"alarm-action", "control"}:
        assert annotations["readOnlyHint"] is False
    if access == "control":
        assert annotations["destructiveHint"] is True


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_capability_is_declared(tool):
    """A tool may only be gated on capabilities the contract actually defines.

    A list, and satisfied by *any* of them: `read_opcua_history` names both
    `history` and `aggregate`, because a server offering only aggregates can
    still answer an aggregate read, and gating it on `history` alone would hide
    the one thing such a server is good at.
    """
    capabilities = tool["capabilities"]
    assert isinstance(capabilities, list), f"{tool['name']}: capabilities must be a list"
    unknown = set(capabilities) - set(CAPABILITIES)
    assert not unknown, (
        f"{tool['name']} gated on unknown capabilities {sorted(unknown)}; "
        f"known: {sorted(CAPABILITIES)}"
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
    referenced = any(tool.get("resultShape") == name for tool in TOOLS) or any(
        resource["body"]["resultShape"] == name for resource in RESOURCES
    )
    assert referenced, f"resultShape {name!r} is defined but nothing declares it"


def record_schema(shape: dict) -> dict:
    """The schema of one record, whether the shape is a list of them or just one.

    Most result shapes are arrays — a tool returns a text block per record. A
    shape that describes a *single* object (`serverStatus`) is the record itself,
    and is held to exactly the same rules below.
    """
    return shape["items"] if shape["type"] == "array" else shape


@pytest.mark.parametrize("name", SHAPE_IDS, ids=SHAPE_IDS)
def test_result_shape_records_are_coherent(name):
    """The record schema must be strict enough for the parity test to enforce it."""
    shape = RESULT_SHAPES[name]
    assert shape["type"] in {"array", "object"}, (
        f"{name} must describe a record or an array of them"
    )
    record = record_schema(shape)
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
    record = record_schema(RESULT_SHAPES["eventRecords"])
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
        "subscribe_events": "eventSubscription",
        "read_events": "eventRecords",
        "list_active_alarms": "eventRecords",
        "acknowledge_alarm": "acknowledgement",
    }


def test_the_history_family_shares_one_result_shape():
    """The divergence in #23 was two tools, both servers; one tool now covers all."""
    history_family = {t["name"]: t.get("resultShape") for t in TOOLS if t["capabilities"]}
    assert history_family == {"read_opcua_history": "historyRecords"}


def test_every_tool_declares_a_result_shape():
    """The systemic fix: the contract pins behaviour, not only interface.

    Ten of the seventeen tools used to declare `resultShape: null`, and for those
    the output format, error wording and defaults were two hand-written copies
    that no test compared. The parity suite could prove the two servers
    *advertise* the same thing; it could not prove they *do* the same thing — and
    four confirmed divergences lived in exactly that gap.

    With a shape on every tool, `tests/e2e/test_contract_parity.py` checks every
    tool's actual output against the contract on both runtimes.
    """
    shapeless = [tool["name"] for tool in TOOLS if not tool.get("resultShape")]
    assert shapeless == [], f"tools with no declared result shape: {shapeless}"


def test_every_declared_shape_exists_and_every_shape_is_used():
    """A shape nothing references is dead, and a reference to nothing is a typo."""
    declared = {name for name in CONTRACT["resultShapes"] if not name.startswith("$")}
    referenced = {tool["resultShape"] for tool in TOOLS if tool.get("resultShape")}
    assert referenced <= declared, f"tools name shapes that do not exist: {referenced - declared}"
    assert declared <= referenced, f"shapes nothing uses: {declared - referenced}"


def test_the_tool_surface_stays_consolidated():
    """13 tools, and the single/batch pairs are gone.

    Not a count for its own sake. Each merged pair was the same operation written
    twice per runtime — four copies — which is *why* the batch read reported
    failures as successes on one runtime (#76) and the browse drained
    continuation points on only one (#75). Re-splitting them would reopen the
    ground those bugs grew in, so the shape of the surface is asserted rather
    than left to review.
    """
    names = {tool["name"] for tool in TOOLS}
    assert len(TOOLS) == 13, sorted(names)
    for retired in (
        "read_opcua_node",
        "read_multiple_opcua_nodes",
        "write_opcua_node",
        "write_multiple_opcua_nodes",
        "browse_opcua_node_children",
        "get_all_variables",
        "read_history_opcua_node",
        "read_aggregate_opcua_node",
        "subscribe_opcua_node",
        "unsubscribe_opcua_node",
    ):
        assert retired not in names, f"{retired} came back"


# --- diagnostics ---------------------------------------------------------------
# `get_server_status` reads two standard nodes. Both servers take the IDs from
# here, so a mistake is a mistake in both at once.


@pytest.mark.parametrize("name", ["serverStatusNodeId", "namespaceArrayNodeId"])
def test_diagnostics_node_ids_are_written_the_way_both_servers_read_them(name):
    assert CONTRACT["diagnostics"][name].startswith("ns=0;i="), CONTRACT["diagnostics"][name]


def test_the_diagnostics_tool_names_the_server_status_shape():
    tool = next(t for t in TOOLS if t["name"] == "get_server_status")
    assert tool["resultShape"] == "serverStatus"
    assert tool["accessClass"] == "read", "a status report must survive an observe-only profile"
    assert tool["capabilities"] == [], "every OPC UA server has ServerStatus"


# --- resources -----------------------------------------------------------------
# Resources carry the live subscription buffers. They are held to the contract
# for the same reason the tools are: a client that has learned one runtime's
# resource surface must find the other's identical.


def test_resource_uris_are_unique():
    assert len(RESOURCE_IDS) == len(set(RESOURCE_IDS)), "duplicate resource URI in the contract"


@pytest.mark.parametrize("resource", RESOURCES, ids=RESOURCE_IDS)
def test_resource_has_required_fields(resource):
    for field in ("uri", "name", "description", "mimeType", "body"):
        assert resource.get(field), f"{resource.get('uri')} is missing {field!r}"


@pytest.mark.parametrize("resource", RESOURCES, ids=RESOURCE_IDS)
def test_resource_body_names_a_declared_shape(resource):
    """The parity test reads the resource and checks it against this shape."""
    body = resource["body"]
    assert body["recordsKey"].strip(), f"{resource['uri']} declares no recordsKey"
    assert body["resultShape"] in RESULT_SHAPES, (
        f"{resource['uri']} declares unknown resultShape "
        f"{body['resultShape']!r}; known: {SHAPE_IDS}"
    )


def test_the_python_server_sources_the_resource_surface_from_the_contract():
    """The Python server must not carry its own copy of a URI or description."""
    from opcua_mcp_server import RESOURCES as PY_RESOURCES

    assert set(PY_RESOURCES) == set(RESOURCE_IDS)
    for resource in RESOURCES:
        assert PY_RESOURCES[resource["uri"]] == resource


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
