"""The canonical configuration schema, and everything that must agree with it (#133).

`contract/config.json` declares every environment variable the servers read. The
distribution metadata is generated from it (`packages/server-node/scripts/
config-artifacts.mjs`), and this module holds the rest of the repository to it:

* the schema is well-formed, and its safety invariants hold — a credential is
  never offered as a command-line flag, every override defaults to off;
* each runtime reads exactly the variables the schema says it reads, found by
  scanning the source rather than by trusting another list;
* the committed `.mcpb` manifest and `server.json` expose every applicable
  setting, with the expected keys computed from the schema, and mark exactly the
  sensitive ones as such (the generator's own `--check`, run by CI and by
  `test/config-schema.test.mjs`, pins the rest of the rendering byte for byte);
* the user-facing configuration docs name every setting and no stale one;
* the Python parsers accept and reject what the schema declares.
  `packages/server-node/test/config-schema.test.mjs` drives the Node parsers
  through the same cases, so the schema is the table both runtimes are held to.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT
from opcua_mcp_server.config import parse_reconnect_config
from opcua_mcp_server.contract import load_config_schema
from opcua_mcp_server.policy import parse_policy_config
from opcua_mcp_server.security import parse_security_config

SCHEMA_PATH = ROOT / "contract" / "config.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
SETTINGS = SCHEMA["settings"]
BY_ENV = {setting["env"]: setting for setting in SETTINGS}

MCPB_MANIFEST = ROOT / "packages" / "server-node" / "mcpb" / "manifest.json"
SERVER_JSON = ROOT / "server.json"

TYPES = {"string", "enum", "boolean", "number", "path", "list"}
RUNTIMES = {"node", "python"}
SURFACES = {"mcpb", "registry", "installer", "docs"}

#: Where each runtime reads its environment. Every file, not a list of modules:
#: a variable read from a new module is exactly the one a hand-kept list misses.
RUNTIME_SOURCES = {
    "node": sorted((ROOT / "packages" / "server-node" / "src").glob("*.ts")),
    "python": sorted(
        (ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server").glob("*.py")
    ),
}

#: An OPCUA_ name, excluding a trailing-underscore prefix such as `OPCUA_RECONNECT_*`.
ENV_NAME = re.compile(r"\bOPCUA_[A-Z0-9_]*[A-Z0-9]\b")


def _surface(surface: str) -> list[dict]:
    return [setting for setting in SETTINGS if surface in setting["surfaces"]]


# --- the schema itself ---------------------------------------------------------


def test_the_packaged_copy_is_the_canonical_schema():
    """What `load_config_schema` finds — the staged copy in a wheel, the repo's in
    a checkout — is this file, so an installer reading it reads the truth."""
    assert load_config_schema() == SCHEMA


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["env"])
def test_every_setting_is_well_formed(setting):
    assert setting["env"].startswith("OPCUA_")
    # The key is derived, not chosen: it names the bundle's user_config field and
    # the installer flag, and a key that could drift from its variable would let
    # the two name different things.
    assert setting["key"] == setting["env"].removeprefix("OPCUA_").lower()
    assert setting["category"] in SCHEMA["categories"]
    assert setting["type"] in TYPES
    assert setting["title"].strip()
    assert setting["description"].strip()
    assert isinstance(setting["required"], bool)
    assert isinstance(setting["secret"], bool)
    assert isinstance(setting["sensitive"], bool)
    assert isinstance(setting["securityRelevant"], bool)
    assert setting["runtimes"] and set(setting["runtimes"]) <= RUNTIMES
    assert set(setting["surfaces"]) <= SURFACES
    assert "default" in setting

    kind = setting["type"]
    default = setting["default"]
    if kind == "enum":
        choices = setting["choices"]
        assert choices and len(set(choices)) == len(choices)
        for runtime, subset in setting.get("runtimeChoices", {}).items():
            assert runtime in setting["runtimes"]
            assert set(subset) < set(choices), f"{runtime} choices must be a proper subset"
        for alias, canonical in setting.get("choiceAliases", {}).items():
            assert canonical in choices and alias not in choices
        assert default is None or default in choices
    elif kind == "boolean":
        assert isinstance(default, bool)
    elif kind == "number":
        assert isinstance(setting["minimum"], (int, float))
        assert default is None or default >= setting["minimum"]
    elif kind == "path":
        assert isinstance(setting["mustExist"], bool)
        assert setting["contents"] in {"certificate", "private-key", "policy-json", "audit-log"}
    elif kind == "list":
        assert setting["itemFormat"] in {"tool-name", "node-id", "object-method-pair"}
    if default is None:
        assert setting.get("defaultDescription") is None or setting["defaultDescription"].strip()


def test_keys_and_names_are_unique():
    assert len({s["env"] for s in SETTINGS}) == len(SETTINGS)
    assert len({s["key"] for s in SETTINGS}) == len(SETTINGS)


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["env"])
def test_credentials_are_handled_as_credentials(setting):
    """A secret is masked everywhere, has no default and no example, and is never
    an installer flag — a flag is a process argument, which any local user can
    read. A private key's *path* is not secret, but it is masked all the same."""
    if setting["secret"]:
        assert setting["sensitive"]
        assert setting["default"] is None
        assert "example" not in setting
        assert "installer" not in setting["surfaces"]
    if setting.get("contents") == "private-key":
        assert setting["sensitive"], f"{setting['env']} points at a private key"


def test_every_override_fails_closed_by_default():
    """Every boolean widens what the server will do, so every one defaults to off,
    and the profile defaults to the one that cannot change anything."""
    for setting in SETTINGS:
        if setting["type"] == "boolean":
            assert setting["default"] is False, setting["env"]
    assert BY_ENV["OPCUA_PROFILE"]["default"] == "observe"
    assert BY_ENV["OPCUA_SECURITY_POLICY"]["default"] == "None"


# --- the runtimes read exactly what the schema declares -----------------------


@pytest.mark.parametrize("runtime", sorted(RUNTIMES))
def test_each_runtime_reads_exactly_the_declared_variables(runtime):
    """Scans the runtime's source for every OPCUA_ name it mentions.

    Both directions: a variable the source reads and the schema omits is one no
    generated artifact can offer; one the schema declares and the source never
    mentions is a setting users can configure to no effect.
    """
    read = set()
    for path in RUNTIME_SOURCES[runtime]:
        read |= set(ENV_NAME.findall(path.read_text(encoding="utf-8")))
    declared = {s["env"] for s in SETTINGS if runtime in s["runtimes"]}
    assert read - declared == set(), f"{runtime} reads variables contract/config.json omits"
    assert declared - read == set(), f"contract/config.json declares variables {runtime} ignores"


# --- the generated artifacts cover the schema ---------------------------------


def test_the_mcpb_manifest_exposes_every_applicable_setting():
    """Claude Desktop passes the server exactly the env the manifest declares, so a
    setting missing here is one a bundle user can never set."""
    manifest = json.loads(MCPB_MANIFEST.read_text(encoding="utf-8"))
    env = manifest["server"]["mcp_config"]["env"]
    user_config = manifest["user_config"]
    expected = _surface("mcpb")

    assert list(env) == [s["env"] for s in expected]
    assert list(user_config) == [f"opcua_{s['key']}" for s in expected]
    for setting in expected:
        field = f"opcua_{setting['key']}"
        assert env[setting["env"]] == "${user_config." + field + "}"
        assert user_config[field].get("sensitive", False) is setting["sensitive"], field
        assert user_config[field]["required"] is setting["required"], field
        if setting["type"] == "boolean":
            assert user_config[field]["default"] is setting["default"], field
        if setting["secret"]:
            assert "default" not in user_config[field], field


def test_server_json_exposes_every_applicable_setting():
    server = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    expected = _surface("registry")
    for package in server["packages"]:
        variables = {v["name"]: v for v in package["environmentVariables"]}
        assert list(variables) == [s["env"] for s in expected]
        for setting in expected:
            variable = variables[setting["env"]]
            assert variable.get("isSecret", False) is setting["sensitive"], setting["env"]
            if setting["secret"]:
                assert "default" not in variable and "placeholder" not in variable
            if setting["default"] is not None:
                # The registry takes every default as a string; `json.dumps`
                # spells a boolean the way the servers parse it.
                default = setting["default"]
                expected = default if isinstance(default, str) else json.dumps(default)
                assert variable["default"] == expected, setting["env"]


# --- the documentation names every setting -------------------------------------

#: Each carries a full configuration table.
REFERENCE_DOCS = [
    ROOT / "README.md",
    ROOT / "packages" / "server-node" / "README.md",
    ROOT / "packages" / "server-python" / "README.md",
]

#: User-facing docs that mention settings in prose; checked for stale names only.
PROSE_DOCS = [
    ROOT / "SECURITY.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "certificates.md",
    ROOT / "docs" / "install.md",
    ROOT / "docs" / "mcp-registry.md",
]


@pytest.mark.parametrize("doc", REFERENCE_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_reference_docs_name_every_setting(doc):
    mentioned = set(ENV_NAME.findall(doc.read_text(encoding="utf-8")))
    missing = {s["env"] for s in _surface("docs")} - mentioned
    assert not missing, f"{doc.name} does not document {sorted(missing)}"


@pytest.mark.parametrize("doc", REFERENCE_DOCS + PROSE_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_docs_name_no_setting_the_servers_do_not_read(doc):
    mentioned = set(ENV_NAME.findall(doc.read_text(encoding="utf-8")))
    unknown = mentioned - set(BY_ENV)
    assert not unknown, f"{doc.name} documents {sorted(unknown)}, which no server reads"


# --- the Python parsers agree with the schema ----------------------------------


def _security(env: dict[str, str]):
    return parse_security_config(env, exists=lambda _path: True)


def _secured(**env: str) -> dict[str, str]:
    """A policy other than None needs a certificate and key; `exists` is faked."""
    return {"OPCUA_CLIENT_CERT": "client.pem", "OPCUA_CLIENT_KEY": "client.key", **env}


def _probe_mode(value: str):
    # A mode is only valid beside a policy that agrees with it.
    policy = "None" if value.strip().lower() == "none" else "Basic256Sha256"
    return _security(_secured(OPCUA_SECURITY_POLICY=policy, OPCUA_SECURITY_MODE=value)).mode


#: How to observe each typed setting through its parser. Not a list of settings —
#: `test_every_typed_setting_has_a_probe` fails when the schema grows one this
#: does not cover.
PROBES = {
    "OPCUA_SECURITY_POLICY": lambda v: _security(_secured(OPCUA_SECURITY_POLICY=v)).policy,
    "OPCUA_SECURITY_MODE": _probe_mode,
    "OPCUA_PROFILE": lambda v: parse_policy_config({"OPCUA_PROFILE": v}).profile,
    "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS": lambda v: (
        parse_policy_config({"OPCUA_ALLOW_ACKNOWLEDGE_ALARMS": v}).acknowledge_alarms
    ),
    "OPCUA_ALLOW_INSECURE_CONTROL": lambda v: (
        parse_policy_config({"OPCUA_ALLOW_INSECURE_CONTROL": v}).allow_insecure_control
    ),
    "OPCUA_ALLOW_OUT_OF_RANGE_WRITES": lambda v: (
        parse_policy_config({"OPCUA_ALLOW_OUT_OF_RANGE_WRITES": v}).allow_out_of_range_writes
    ),
    "OPCUA_RECONNECT_INITIAL_DELAY_MS": lambda v: (
        parse_reconnect_config({"OPCUA_RECONNECT_INITIAL_DELAY_MS": v}).initial_delay_ms
    ),
    "OPCUA_RECONNECT_MAX_DELAY_MS": lambda v: (
        parse_reconnect_config({"OPCUA_RECONNECT_MAX_DELAY_MS": v}).max_delay_ms
    ),
    "OPCUA_RECONNECT_MAX_RETRY": lambda v: (
        parse_reconnect_config({"OPCUA_RECONNECT_MAX_RETRY": v}).max_retry
    ),
    "OPCUA_SESSION_TIMEOUT_MS": lambda v: (
        parse_reconnect_config({"OPCUA_SESSION_TIMEOUT_MS": v}).session_timeout_ms
    ),
}

TYPED = [s for s in SETTINGS if s["type"] in {"enum", "boolean", "number"}]


def test_every_typed_setting_has_a_probe():
    assert set(PROBES) == {s["env"] for s in TYPED if "python" in s["runtimes"]}


def _cases(setting: dict) -> tuple[list[tuple[str, object]], list[str]]:
    """(raw value, expected parse) pairs to accept, and raw values to refuse."""
    kind = setting["type"]
    if kind == "enum":
        allowed = setting.get("runtimeChoices", {}).get("python", setting["choices"])
        accept = [(c, c) for c in allowed] + [(c.upper(), c) for c in allowed]
        accept += list(setting.get("choiceAliases", {}).items())
        refuse = ["not-a-choice", *(c for c in setting["choices"] if c not in allowed)]
        return accept, refuse
    if kind == "boolean":
        values = SCHEMA["booleanValues"]
        accept = [(v, True) for v in values["true"]] + [(v, False) for v in values["false"]]
        accept += [(v.upper(), True) for v in values["true"]]
        return accept, ["maybe", "2"]
    minimum = setting["minimum"]
    return [(str(minimum), minimum), (str(minimum + 0.5), minimum + 0.5)], [
        str(minimum - 1),
        "abc",
        "inf",
    ]


@pytest.mark.parametrize("setting", TYPED, ids=lambda s: s["env"])
def test_the_python_parser_accepts_what_the_schema_declares(setting):
    probe = PROBES[setting["env"]]
    accept, _ = _cases(setting)
    for raw, expected in accept:
        assert probe(raw) == expected, f"{setting['env']}={raw!r}"


@pytest.mark.parametrize("setting", TYPED, ids=lambda s: s["env"])
def test_the_python_parser_refuses_what_the_schema_does_not_declare(setting):
    probe = PROBES[setting["env"]]
    _, refuse = _cases(setting)
    for raw in refuse:
        with pytest.raises(ValueError):
            probe(raw)
    # A value outside every runtime's vocabulary is refused naming the variable,
    # so an operator knows which line of the config to fix.
    with pytest.raises(ValueError, match=setting["env"]):
        probe(refuse[0])


@pytest.mark.parametrize(
    "setting", [s for s in TYPED if s["default"] is not None], ids=lambda s: s["env"]
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_value_means_the_declared_default(setting, blank):
    """MCP clients pass unset optional fields as blank strings, so blank must mean
    "not configured" — and "not configured" must mean what the schema says."""
    assert PROBES[setting["env"]](blank) == setting["default"]


@pytest.mark.parametrize("runtime", sorted(RUNTIMES))
def test_a_blank_endpoint_means_the_declared_default(runtime):
    """The endpoint is read at import, so it is probed in a fresh process.

    The Python runtime once read `OPCUA_SERVER_URL=""` as an endpoint of `""`
    where the Node runtime fell back to the default — the one blank-handling
    difference building this schema turned up.
    """
    env = {**os.environ, "OPCUA_SERVER_URL": ""}
    if runtime == "python":
        cmd = [
            sys.executable,
            "-c",
            "from opcua_mcp_server.config import SERVER_URL as u; print(u)",
        ]
    else:
        node = shutil.which("node")
        config_js = ROOT / "packages" / "server-node" / "build" / "config.js"
        if node is None or not config_js.is_file():
            pytest.skip("Node runtime not built — run `npm run build` in packages/server-node")
        script = (
            f"const m = await import({json.dumps(config_js.as_uri())}); console.log(m.SERVER_URL)"
        )
        cmd = [node, "--input-type=module", "-e", script]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == BY_ENV["OPCUA_SERVER_URL"]["default"]
