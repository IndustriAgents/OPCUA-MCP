"""Structural invariants of the shared tool contract.

contract/tools.json is the single source of truth both servers derive from, so a
malformed entry breaks both at once. The e2e parity test catches drift *between*
the servers but needs a running mock server and can only compare what the servers
actually advertised; these checks are on the file itself and run in milliseconds.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import contract_candidates, load_contract

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))
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
        # What this server may do when the session dies under the request. Every
        # tool must say; see tests/unit/test_retry_policy.py for what each value
        # means and why it is not `annotations.idempotentHint`.
        "retryPolicy",
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
    _assert_object_schema_is_strict(record_schema(shape), name)


def _assert_object_schema_is_strict(schema: dict, where: str) -> None:
    """Every field required, nothing extra allowed, everything documented.

    Applied to nested objects too, not only the record: `nodeValues.engineering`
    is an object of objects, and a nested field with no description or a nested
    schema that allowed extras would be a place the two servers could diverge
    with nothing looking.
    """
    assert "object" in _declared_types(schema), f"{where} must describe an object"
    properties = schema["properties"]
    assert set(schema["required"]) == set(properties), (
        f"{where}: every field must be required, so neither server may omit one"
    )
    assert schema.get("additionalProperties") is False, (
        f"{where}: extra fields must be forbidden, or the servers can still diverge"
    )
    for field, spec in properties.items():
        assert spec.get("description", "").strip(), (
            f"{where}.{field} has no description — the model relies on it"
        )
        if "properties" in spec:
            _assert_object_schema_is_strict(spec, f"{where}.{field}")


def _declared_types(schema: dict) -> set[str]:
    """A schema's `type`, whether it is a name or a union with null."""
    declared = schema.get("type")
    return {declared} if isinstance(declared, str) else set(declared or [])


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


# --- the error surface ------------------------------------------------------


ERROR_TEMPLATES = {k: v for k, v in CONTRACT["errors"].items() if not k.startswith("$")}
ERROR_IDS = sorted(ERROR_TEMPLATES)


@pytest.mark.parametrize("key", ERROR_IDS)
def test_error_template_is_a_non_empty_sentence(key):
    template = ERROR_TEMPLATES[key]
    assert isinstance(template, str) and template.strip(), f"errors.{key} is empty"
    # `{` and `}` only ever delimit a placeholder here: both runtimes substitute
    # by pattern, so a stray brace would be silently left in an operator's face.
    assert template.count("{") == template.count("}"), f"errors.{key} has an unbalanced brace"


@pytest.mark.parametrize("key", ERROR_IDS)
def test_error_template_is_used_by_both_runtimes(key):
    """Every template is reached from both servers, or it is dead wording.

    A template only one runtime uses is the divergence this block was added to
    remove, reintroduced one key at a time.
    """
    python_sources = " ".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server").glob("*.py")
    )
    node_sources = " ".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "packages" / "server-node" / "src").glob("*.ts")
    )
    assert f'"{key}"' in python_sources, f"errors.{key} is never used by the Python server"
    assert f'"{key}"' in node_sources, f"errors.{key} is never used by the Node server"


def _python_string_literals(path: Path) -> list[str]:
    """Every string constant in a Python file that is not a docstring.

    Parsed rather than grepped: the first version of this check was a substring
    scan and it flagged the *docstring* that explains the bug, which is precisely
    the kind of false positive that gets a useful test deleted.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _typescript_string_literals(path: Path) -> list[str]:
    """Every quoted or backticked run in a TypeScript file, comments removed.

    Crude next to a parser, and enough: block comments and `//` lines are what
    carry the prose that would otherwise look like an inlined message.
    """
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    matches = re.findall(r"\"([^\"\n]*)\"|'([^'\n]*)'|`([^`]*)`", text)
    return [group for match in matches for group in match if group]


#: Fragments that must only ever reach a client through contract.errors. Each was
#: a literal in both runtimes before the errors block existed.
SHARED_MESSAGE_FRAGMENTS = (
    "Failed to read nodes",
    "Failed to write nodes",
    "No such subscription",
    "is disabled by OPCUA_PROFILE",
    "is not writable under the operator policy",
)


def test_no_runtime_still_carries_its_own_copy_of_a_message():
    """The literals the errors block replaced must not grow back.

    Checked as source rather than by behaviour because that is how they came back
    last time: a message is easy to inline again while every test still passes,
    and the framing divergence this closes — one runtime saying "Error executing
    tool X: ..." where the other said the bare sentence — was invisible for
    exactly that reason.
    """
    sources = [
        (path, _python_string_literals(path))
        for path in (ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server").glob("*.py")
        if path.name != "errors.py"
    ] + [
        (path, _typescript_string_literals(path))
        for path in (ROOT / "packages" / "server-node" / "src").glob("*.ts")
        if path.name != "errors.ts"
    ]

    offenders = [
        f"{path.name}: {literal!r}"
        for path, literals in sources
        for literal in literals
        for fragment in SHARED_MESSAGE_FRAGMENTS
        if fragment in literal
    ]
    assert not offenders, (
        f"these messages are inlined again instead of coming from contract.errors: {offenders}"
    )


def test_no_test_reads_a_file_at_the_system_locale():
    """`read_text()` without an encoding is a Windows-only failure waiting to happen.

    `Path.read_text` defaults to the system locale, which on Windows is cp1252 —
    so the em dashes and ellipses throughout `contract/tools.json` come back as
    replacement characters and every description comparison fails. The *product*
    has always known this: `contract.py` passes `encoding="utf-8"` explicitly and
    says why. The tests did not, and nothing noticed until CI first ran on
    Windows, because on macOS and Linux the locale happens to be UTF-8.

    Checked here rather than left to the cross-platform job, which is not a
    required check and runs on two of the four jobs: this makes the mistake fail
    everywhere, immediately, for the price of a grep.
    """
    # Assembled rather than written out, so this line is not itself a hit.
    bare_read = ".read_text" + "()"
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in (ROOT / "tests").rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if bare_read in line
    ]
    assert not offenders, (
        f"these read a file at the system locale and will fail on Windows; "
        f'pass encoding="utf-8": {offenders}'
    )
