"""Every ``--install`` case in ``tests/fixtures/install-cases.json``, through both CLIs.

The installer is the easiest way to configure this server, so what it will write,
refuse and warn about is security behaviour (#135). Both runtimes run each case as
a user would — the real command line, in a subprocess, with HOME pointed at a
scratch directory so no developer's config is ever read or written — and must
produce the case's configuration, exit code, error code and warnings exactly.
``packages/server-node/test/install-cases.test.mjs`` drives the same table
through the Node planner in-process.

Everything runs as ``--dry-run``, so the configuration is read back from the
redacted preview: sensitive values are compared as ``<redacted>``, which is also
the check that the preview redacts them. ``test_install_parity.py`` covers the
real writes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT
from opcua_mcp_server.install import REDACTED, redact_env

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

NODE_BUILD = ROOT / "packages" / "server-node" / "build" / "index.js"
CASES = json.loads((ROOT / "tests" / "fixtures" / "install-cases.json").read_text("utf-8"))["cases"]

COMMANDS = {
    "python": [sys.executable, "-m", "opcua_mcp_server"],
    "node": ["node", str(NODE_BUILD)],
}


def make_pki(root: Path) -> Path:
    """The scratch directory the table's `{pki}` stands for. Contents are dummies:
    the installer checks that certificates exist, and parses only the policy file."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("server.pem", "client.pem", "client_key.pem", "user.pem", "user_key.pem"):
        (root / name).write_text("placeholder\n", encoding="utf-8")
    policy = {"version": 1, "profile": "operator", "control": {"writable_nodes": ["ns=2;i=5"]}}
    (root / "policy-operator.json").write_text(json.dumps(policy), encoding="utf-8")
    (root / "policy-bad.json").write_text("{ not json", encoding="utf-8")
    (root / "audit").mkdir(exist_ok=True)
    # Resolved, because the installers resolve relative paths against the working
    # directory as the OS reports it — /private/var/... on macOS, not /var/....
    return Path(os.path.realpath(root))


def substitute(value, pki: Path):
    """`{pki}` replaced throughout a JSON value, in the platform's path spelling."""
    if isinstance(value, str):
        return value.replace("{pki}/", str(pki) + os.sep).replace("{pki}", str(pki))
    if isinstance(value, list):
        return [substitute(v, pki) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, pki) for k, v in value.items()}
    return value


def scratch_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for one run: a scratch HOME, and no OPCUA_* from the shell."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPCUA_")}
    env.pop("CODEX_HOME", None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "XDG_CONFIG_HOME": str(home / ".config"),
        }
    )
    env.update(extra or {})
    return env


def run_cli(impl: str, args: list[str], home: Path, cwd: Path, env=None):
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return subprocess.run(
        COMMANDS[impl] + args,
        capture_output=True,
        text=True,
        timeout=120,
        env=scratch_env(home, env),
        cwd=cwd,
    )


def previewed_entry(client: str, stdout: str) -> dict:
    """This server's entry, parsed out of a `--dry-run` preview."""
    body = stdout[stdout.index("\n") + 1 :]
    if client == "codex":
        if tomllib is None:
            pytest.skip("tomllib needs Python 3.11")
        return tomllib.loads(body)["mcp_servers"]["opcua"]
    return json.loads(body)["mcpServers"]["opcua"]


def expectation(case: dict, impl: str) -> dict:
    return {**case["expect"], **case.get("byRuntime", {}).get(impl, {})}


@pytest.mark.parametrize("impl", ["python", "node"])
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_install_case(case, impl, tmp_path):
    pki = make_pki(tmp_path / "pki")
    client = case.get("client", "claude-desktop")
    args = ["--install", client, "--dry-run", *substitute(case["args"], pki)]
    proc = run_cli(impl, args, tmp_path / "home", pki, case.get("env"))
    output = proc.stdout + proc.stderr
    expect = substitute(expectation(case, impl), pki)

    assert proc.returncode == expect["exit"], f"{impl}: {output}"
    for secret in case.get("neverPrinted", []):
        assert secret not in output, f"{impl} printed {secret!r}"
    assert not list((tmp_path / "home").rglob("*")), f"{impl} wrote during --dry-run"

    codes = re.findall(r"^Error \[([a-z-]+)\]:", proc.stderr, re.M)
    if expect["exit"] != 0:
        assert proc.stdout == "", f"{impl} previewed a config it refused"
        assert codes == ([expect["error"]] if expect.get("error") else []), output
        return

    assert codes == []
    warnings = re.findall(r"^WARNING \[([a-z-]+)\]:", proc.stderr, re.M)
    assert warnings == expect["warnings"], output
    assert "Security summary:" in proc.stderr

    entry = previewed_entry(client, proc.stdout)
    assert entry["env"] == redact_env(expect["env"])
    assert list(entry["env"]) == list(expect["env"]), "not in schema order"
    assert entry.get("env_vars", []) == expect.get("envVars", [])


def test_sensitive_values_are_redacted_in_the_table_itself():
    """A guard on the guard: at least one case must carry a value the preview
    has to hide, or the redaction comparison above proves nothing."""
    hidden = [
        c
        for c in CASES
        if c["expect"].get("env") and REDACTED in redact_env(c["expect"]["env"]).values()
    ]
    assert hidden
