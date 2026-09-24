"""The declared list of what the two runtimes do not share.

ADR 0001 (docs/adr/0001-two-first-class-runtimes.md) makes both runtimes
first-class: the same contract, the same suite, the same release gate. Some
differences are real and cannot be engineered away — a security policy one
client library lacks, an MCP SDK a protocol generation behind — and the ADR's
rule for those is that they are *declared*, in
contract/runtime-differences.json, never left to be discovered.

A list like that goes stale in both directions: a difference gets fixed and the
entry lingers, or one grows and nobody adds it. So besides the shape of each
entry, these tests check every claim the repository can answer for itself — the
two policy lists, the manifests, the bundle, the registry file, the installed
commands, and the runtime-specific settings contract/config.json records —
against the entry that makes it. A fact that changes fails here and
sends whoever changed it to the list, the docs and the changelog. What cannot be
checked statically (how a reconnect behaves, what a library writes to disk) is
held to the weaker rule that it states why it is deliberate.

Accidental divergences are not listed at all — they are bugs, tracked in #157
and #136 — so nothing in this file can make one look allowed.
"""

from __future__ import annotations

import json
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

import pytest
from conftest import ROOT
from opcua_mcp_server import security

DIFFERENCES_FILE = ROOT / "contract" / "runtime-differences.json"
DOC = ROOT / "docs" / "compatibility.md"
NODE_PKG = ROOT / "packages" / "server-node" / "package.json"
NODE_SECURITY = ROOT / "packages" / "server-node" / "src" / "security.ts"
PYTHON_PYPROJECT = ROOT / "packages" / "server-python" / "pyproject.toml"
MCPB_MANIFEST = ROOT / "packages" / "server-node" / "mcpb" / "manifest.json"
SERVER_JSON = ROOT / "server.json"
CONFIG = json.loads((ROOT / "contract" / "config.json").read_text(encoding="utf-8"))

DIFFERENCES = json.loads(DIFFERENCES_FILE.read_text(encoding="utf-8"))
ENTRIES = DIFFERENCES["runtimeDifferences"]
IDS = [entry["id"] for entry in ENTRIES]
BY_ID = {entry["id"]: entry for entry in ENTRIES}

AREAS = {
    "security",
    "connection",
    "configuration",
    "protocol",
    "errors",
    "distribution",
    "cli",
    "filesystem",
    "performance",
    "dependencies",
}
# Every entry is deliberate, so every entry says why. There is deliberately no
# field for "known bug": an accidental divergence is fixed, and tracked in an
# issue (#157, #136) until it is — declaring it here would make it allowed.
REQUIRED_KEYS = {
    "id",
    "area",
    "summary",
    "python",
    "node",
    "observable",
    "rationale",
    "evidence",
}
OPTIONAL_KEYS = {"tracking", "facts"}
ISSUE_REF = re.compile(r"^#[1-9][0-9]*$")
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _facts(entry_id: str) -> dict:
    """The checkable facts an entry declares, or {} if there is no such entry.

    Absent means "no difference here", which is itself a claim the fact checks
    test: delete the AES entry while the runtimes still disagree and the policy
    check below fails, rather than silently checking nothing.
    """
    return BY_ID.get(entry_id, {}).get("facts", {})


# --- the file itself ----------------------------------------------------------


def test_top_level_shape():
    assert set(DIFFERENCES) == {
        "$comment",
        "schemaVersion",
        "adr",
        "runtimes",
        "runtimeDifferences",
    }
    assert DIFFERENCES["schemaVersion"] == 1
    assert (ROOT / DIFFERENCES["adr"]).is_file(), DIFFERENCES["adr"]
    assert set(DIFFERENCES["runtimes"]) == {"python", "node"}
    for name, runtime in DIFFERENCES["runtimes"].items():
        assert set(runtime) == {"package", "registry", "minimum", "mcpSdk", "opcuaLibrary"}, name
        assert all(isinstance(v, str) and v.strip() for v in runtime.values()), name


def test_ids_are_unique_kebab_case():
    assert len(IDS) == len(set(IDS)), "duplicate id in runtimeDifferences"
    for entry_id in IDS:
        assert KEBAB.match(entry_id), f"{entry_id!r} is not kebab-case"


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_shape(entry):
    keys = set(entry)
    allowed = REQUIRED_KEYS | OPTIONAL_KEYS
    assert keys >= REQUIRED_KEYS, f"missing {sorted(REQUIRED_KEYS - keys)}"
    assert keys <= allowed, f"unknown {sorted(keys - allowed)}"
    assert entry["area"] in AREAS, entry["area"]
    assert isinstance(entry["observable"], bool)
    for key in ("summary", "python", "node"):
        assert isinstance(entry[key], str) and entry[key].strip(), key
    assert entry["python"] != entry["node"], "a difference must say what differs"


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_is_accounted_for(entry):
    """Every difference says why it is deliberate, and may name the issue that
    would retire it (typically #144, the library migration)."""
    rationale = entry["rationale"]
    tracking = entry.get("tracking", [])
    assert isinstance(rationale, str) and rationale.strip(), "say why it is deliberate"
    assert isinstance(tracking, list)
    for ref in tracking:
        assert ISSUE_REF.match(ref), f"{ref!r} is not an issue reference like '#144'"


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_evidence_exists(entry):
    assert entry["evidence"], "name at least one file that shows the difference"
    for path in entry["evidence"]:
        assert (ROOT / path).exists(), f"{path} does not exist"


def test_every_difference_is_documented():
    """docs/compatibility.md renders the list for people, one row per id, both ways."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Runtime differences", 1)
    assert len(section) == 2, "docs/compatibility.md has no '## Runtime differences' section"
    body = section[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([a-z0-9-]+)` \|", body, flags=re.MULTILINE))
    assert documented == set(IDS), (
        f"undocumented: {sorted(set(IDS) - documented)}; "
        f"documented but not declared: {sorted(documented - set(IDS))}"
    )


# --- the facts the repository can check -----------------------------------------


def _floor(spec: str) -> str:
    match = re.fullmatch(r">=\s*([0-9.]+)", spec.strip())
    assert match, f"expected a bare '>=' floor, got {spec!r}"
    return match.group(1)


def test_runtime_floors_match_the_manifests_and_the_adr():
    pyproject = tomllib.loads(PYTHON_PYPROJECT.read_text(encoding="utf-8"))
    package = json.loads(NODE_PKG.read_text(encoding="utf-8"))
    mcpb = json.loads(MCPB_MANIFEST.read_text(encoding="utf-8"))
    runtimes = DIFFERENCES["runtimes"]

    assert runtimes["python"]["minimum"] == _floor(pyproject["project"]["requires-python"])
    assert runtimes["node"]["minimum"] == _floor(package["engines"]["node"])
    # The bundle asks Claude Desktop for a Node; it has to be the same floor.
    assert _floor(mcpb["compatibility"]["runtimes"]["node"]) == runtimes["node"]["minimum"]

    adr = (ROOT / DIFFERENCES["adr"]).read_text(encoding="utf-8")
    python_floor = runtimes["python"]["minimum"]
    node_floor = ".".join(runtimes["node"]["minimum"].split(".")[:2])
    assert f"**{python_floor}**" in adr, f"ADR 0001 does not state Python {python_floor}"
    assert f"**{node_floor}**" in adr, f"ADR 0001 does not state Node {node_floor}"


def _ts_string_array(source: str, name: str) -> list[str]:
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", source, flags=re.DOTALL)
    assert match, f"{name} not found in security.ts"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_security_policies_differ_only_as_declared():
    node_source = NODE_SECURITY.read_text(encoding="utf-8")
    node_shared = _ts_string_array(node_source, "SHARED_POLICIES")
    node_only = _ts_string_array(node_source, "NODE_ONLY_POLICIES")
    declared = _facts("security-policies-aes").get("nodeOnlyPolicies", [])

    assert list(security.POLICIES) == node_shared, "the shared policy lists disagree"
    assert node_only == declared
    # Python names the Node-only policies in its refusal, so the two lists have
    # to agree for that message to be true.
    assert list(security.NODE_ONLY_POLICIES) == declared


def _runtime_specific_settings() -> dict[str, dict]:
    """The settings contract/config.json marks as differing between runtimes.

    Two ways a setting can: only one runtime reads it (`runtimes`), or one
    runtime accepts fewer of its choices (`runtimeChoices`).
    """
    return {
        setting["env"]: setting
        for setting in CONFIG["settings"]
        if set(setting["runtimes"]) != {"python", "node"} or setting.get("runtimeChoices")
    }


def test_config_schema_and_declared_differences_agree():
    """The configuration contract and this list describe the same differences.

    contract/config.json records runtime-specific settings for the generated
    bundle form and registry entry; this file records why they differ. A
    setting marked runtime-specific there and not declared here is an
    undeclared difference, and one declared here that config.json treats as
    shared is a stale entry — either way, one of the two is wrong.
    """
    in_config = set(_runtime_specific_settings())
    declared: list[str] = []
    for entry in ENTRIES:
        declared.extend(entry.get("facts", {}).get("configSettings", []))
    assert len(declared) == len(set(declared)), "a setting is claimed by two entries"
    assert set(declared) == in_config, (
        f"runtime-specific in config.json but undeclared: {sorted(in_config - set(declared))}; "
        f"declared but shared in config.json: {sorted(set(declared) - in_config)}"
    )


def test_config_schema_policy_choices_match_the_declared_policies():
    policy = _runtime_specific_settings()["OPCUA_SECURITY_POLICY"]
    python_only_accepts = policy["runtimeChoices"]["python"]
    # Node accepts every choice; only Python is narrowed.
    assert set(policy["runtimeChoices"]) == {"python"}
    node_only = [c for c in policy["choices"] if c not in python_only_accepts]
    assert node_only == _facts("security-policies-aes")["nodeOnlyPolicies"]
    assert python_only_accepts == list(security.POLICIES)


def _major(spec: str) -> int:
    match = re.search(r"(?:\^|>=)\s*([0-9]+)", spec)
    assert match, f"cannot find a major version in {spec!r}"
    return int(match.group(1))


def test_mcp_sdk_generations_are_as_declared():
    pyproject = tomllib.loads(PYTHON_PYPROJECT.read_text(encoding="utf-8"))
    package = json.loads(NODE_PKG.read_text(encoding="utf-8"))
    mcp_spec = next(d for d in pyproject["project"]["dependencies"] if d.startswith("mcp"))
    facts = _facts("mcp-protocol-generation")

    python_major = _major(mcp_spec)
    node_major = _major(package["dependencies"]["@modelcontextprotocol/sdk"])
    # Moving either SDK to a new major is exactly when this entry needs rereading.
    assert facts.get("pythonSdkMajor") == python_major
    assert facts.get("nodeSdkMajor") == node_major


def test_mcpb_bundle_is_node_only():
    mcpb = json.loads(MCPB_MANIFEST.read_text(encoding="utf-8"))
    assert mcpb["server"]["type"] == _facts("mcpb-bundle")["bundleRuntime"] == "node"
    assert not (ROOT / "packages" / "server-python" / "mcpb").exists()


def test_registry_listing_is_as_declared():
    listing = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    types = sorted({p["registryType"] for p in listing["packages"]})
    assert types == _facts("mcp-registry-listing").get("registryTypes")


def test_installed_commands_differ_only_as_declared():
    pyproject = tomllib.loads(PYTHON_PYPROJECT.read_text(encoding="utf-8"))
    package = json.loads(NODE_PKG.read_text(encoding="utf-8"))
    python_commands = set(pyproject["project"]["scripts"])
    node_commands = set(package["bin"])

    assert "opcua-mcp-server" in python_commands & node_commands
    assert python_commands <= node_commands, "a Python-only command is undeclared"
    assert sorted(node_commands - python_commands) == _facts("cli-command-alias").get(
        "nodeOnlyCommands", []
    )
