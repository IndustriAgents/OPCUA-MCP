"""``--install`` must behave identically in both runtimes.

The two servers are documented as interchangeable and are installed under the
same command name, so a user who follows the README with either one has to end up
with a working Claude Desktop entry and the same reported version. The per-runtime
details are covered by ``unit/test_install.py`` and
``packages/server-node/test/install.test.mjs``; this drives the *actual* commands
and compares what comes out.

Needs no OPC UA server — that is the point of the lazy import in
``opcua_mcp_server/cli.py`` — so it lives with the unit tests and runs in the fast
CI job rather than the end-to-end one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import load_config_schema

NODE_BUILD = ROOT / "packages" / "server-node" / "build" / "index.js"

URL = "opc.tcp://parity:4840"

# Both runtimes are invoked exactly as a user would invoke the installed command.
COMMANDS = {
    "python": [sys.executable, "-m", "opcua_mcp_server"],
    "node": ["node", str(NODE_BUILD)],
}


def _run(
    impl: str, args: list[str], home, extra_env: dict | None = None, cwd=None
) -> subprocess.CompletedProcess:
    """Run one runtime's CLI with HOME redirected at a scratch directory.

    Both runtimes resolve the Claude Desktop config path from the home directory,
    so pointing HOME (and APPDATA, for Windows) somewhere disposable keeps the
    test from going anywhere near a developer's real config.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    # The default must come from the code, not the shell; and Codex's location
    # must come from HOME, not from a developer's CODEX_HOME.
    for name in [n for n in env if n.startswith("OPCUA_")] + ["CODEX_HOME"]:
        env.pop(name, None)
    env.update(extra_env or {})
    return subprocess.run(
        COMMANDS[impl] + args, capture_output=True, text=True, timeout=120, env=env, cwd=cwd
    )


def _dry_run_config(impl: str, home, extra: list[str] | None = None) -> dict:
    """The config `--install --dry-run` would write, parsed out of its output."""
    proc = _run(impl, ["--install", "claude-desktop", "--dry-run", *(extra or [])], home)
    assert proc.returncode == 0, f"{impl}: {proc.stderr}"
    return json.loads(proc.stdout[proc.stdout.index("{") :])


def _config_target(impl: str, home) -> Path:
    """Where this runtime would write, taken from its own `--dry-run` report.

    Asking rather than reconstructing: the path is platform-specific (Application
    Support on macOS, XDG on Linux, APPDATA on Windows), and a test that hardcodes
    one of them silently stops testing anything on the other two — it seeds a file
    the runtime never reads, and then asserts against whatever it does instead.
    """
    proc = _run(impl, ["--install", "claude-desktop", "--dry-run"], home)
    assert proc.returncode == 0, proc.stderr
    first_line = proc.stdout.splitlines()[0]
    assert first_line.startswith("Would write "), f"{impl} changed its dry-run preamble"
    return Path(first_line.removeprefix("Would write ").rstrip(":"))


@pytest.mark.parametrize("impl", ["python", "node"])
def test_install_writes_a_launchable_entry(impl, tmp_path):
    """Whatever the runtime, the entry must name an absolute command with the
    endpoint in its environment — the thing Claude Desktop will actually spawn."""
    entry = _dry_run_config(impl, tmp_path, ["--url", URL])["mcpServers"]["opcua"]

    assert entry["env"] == {"OPCUA_SERVER_URL": URL, "OPCUA_PROFILE": "observe"}
    assert os.path.isabs(entry["command"]), (
        f"{impl} recorded a bare command ({entry['command']!r}); Claude Desktop is "
        "launched from the GUI and does not inherit a login shell's PATH"
    )


def test_both_runtimes_record_the_same_endpoint(tmp_path):
    """The one field a user actually sets has to mean the same thing in both."""
    python_entry = _dry_run_config("python", tmp_path / "py", ["--url", URL])["mcpServers"]["opcua"]
    node_entry = _dry_run_config("node", tmp_path / "node", ["--url", URL])["mcpServers"]["opcua"]

    assert python_entry["env"] == node_entry["env"]
    assert set(python_entry) <= {"command", "args", "env"}
    assert set(node_entry) <= {"command", "args", "env"}


@pytest.mark.parametrize("impl", ["python", "node"])
def test_the_default_endpoint_is_the_documented_one(impl, tmp_path):
    """With no --url and no OPCUA_SERVER_URL, both must fall back to the default
    the README documents, not to something runtime-specific."""
    entry = _dry_run_config(impl, tmp_path)["mcpServers"]["opcua"]
    assert entry["env"]["OPCUA_SERVER_URL"] == "opc.tcp://localhost:4840"


@pytest.mark.parametrize("impl", ["python", "node"])
def test_dry_run_writes_nothing(impl, tmp_path):
    _dry_run_config(impl, tmp_path)
    assert not list(tmp_path.rglob("claude_desktop_config.json"))


@pytest.mark.parametrize("impl", ["python", "node"])
def test_install_is_idempotent_only_with_force(impl, tmp_path):
    """A second install must refuse rather than silently rewrite, and --force must
    then succeed — the same contract in both runtimes."""
    assert _run(impl, ["--install", "claude-desktop"], tmp_path).returncode == 0

    written = list(tmp_path.rglob("claude_desktop_config.json"))
    assert len(written) == 1, f"{impl} wrote {written}"

    again = _run(impl, ["--install", "claude-desktop"], tmp_path)
    assert again.returncode == 1
    assert "already configured" in (again.stdout + again.stderr)

    forced = _run(impl, ["--install", "claude-desktop", "--force"], tmp_path)
    assert forced.returncode == 0, forced.stderr


@pytest.mark.parametrize("impl", ["python", "node"])
def test_help_and_version_go_to_stdout(impl, tmp_path):
    """`--help` and `--version` are the program's output, not diagnostics, and the
    Node runtime routes stray `console.log` to stderr — so this is a real trap."""
    helped = _run(impl, ["--help"], tmp_path)
    assert helped.returncode == 0
    assert "--install" in helped.stdout

    versioned = _run(impl, ["--version"], tmp_path)
    assert versioned.returncode == 0
    assert versioned.stdout.strip()


def test_both_runtimes_report_the_same_version(tmp_path):
    python_version = _run("python", ["--version"], tmp_path).stdout.strip()
    node_version = _run("node", ["--version"], tmp_path).stdout.strip()
    assert python_version == node_version


@pytest.mark.parametrize("impl", ["python", "node"])
def test_an_unknown_argument_exits_2_without_serving(impl, tmp_path):
    """Exit 2 is the conventional usage error, and — more importantly — an
    unrecognised flag must not fall through into starting the MCP server."""
    proc = _run(impl, ["--frobnicate"], tmp_path)
    assert proc.returncode == 2, proc.stderr


@pytest.mark.parametrize("impl", ["python", "node"])
def test_an_unknown_client_is_rejected(impl, tmp_path):
    assert _run(impl, ["--install", "emacs"], tmp_path).returncode == 2


# --- malformed input must fail the same way in both -----------------------------


@pytest.mark.parametrize("impl", ["python", "node"])
def test_a_flag_is_never_swallowed_as_another_flags_value(impl, tmp_path):
    """`--url --dry-run` must be rejected, not read as an endpoint of "--dry-run".

    The Node parser used to take the next token unconditionally, so a forgotten
    endpoint both produced a nonsense URL *and* consumed the flag that was meant
    to stop anything being written — the config got rewritten for real, and the
    command reported success. Python's argparse always refused it; the point of
    this test is that the two agree.
    """
    proc = _run(impl, ["--install", "claude-desktop", "--url", "--dry-run"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not list(tmp_path.rglob("claude_desktop_config.json")), (
        f"{impl} wrote a config despite a usage error"
    )


@pytest.mark.parametrize("impl", ["python", "node"])
def test_a_missing_client_name_is_rejected(impl, tmp_path):
    proc = _run(impl, ["--install", "--dry-run"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not list(tmp_path.rglob("claude_desktop_config.json"))


@pytest.mark.parametrize("impl", ["python", "node"])
def test_dry_run_output_survives_a_pipe(impl, tmp_path):
    """A large `--dry-run` must not be truncated when stdout is a pipe.

    Node's writes to a pipe are asynchronous and `process.exit()` does not wait
    for them, so this used to stop at exactly one 64 KB pipe buffer — emitting
    invalid JSON and still exiting 0. `subprocess` gives the child a pipe, which
    is precisely the condition that triggers it; a shell redirect to a file would
    not have caught it.
    """
    target = _config_target(impl, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    bulk = {"command": "x" * 200, "args": ["y" * 200]}
    existing = {"mcpServers": {f"server{i}": bulk for i in range(2000)}}
    target.write_text(json.dumps(existing), encoding="utf-8")

    proc = _run(impl, ["--install", "claude-desktop", "--dry-run", "--force"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) > 100_000, "test is meaningless below one pipe buffer"

    printed = json.loads(proc.stdout[proc.stdout.index("{") :])
    assert len(printed["mcpServers"]) == len(existing["mcpServers"]) + 1


@pytest.mark.parametrize("impl", ["python", "node"])
def test_flag_equals_value_is_accepted(impl, tmp_path):
    """`--url=...` as well as `--url ...`.

    argparse has always taken both forms, so a user following the Python docs at
    the Node runtime must not be told `--install=claude-desktop` is an unknown
    argument. The Node parser only matched whole tokens until this was noticed.
    """
    proc = _run(impl, ["--install=claude-desktop", f"--url={URL}", "--dry-run"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    entry = json.loads(proc.stdout[proc.stdout.index("{") :])["mcpServers"]["opcua"]
    assert entry["env"]["OPCUA_SERVER_URL"] == URL


@pytest.mark.parametrize("impl", ["python", "node"])
def test_a_value_on_a_boolean_flag_is_rejected(impl, tmp_path):
    proc = _run(impl, ["--install=claude-desktop", "--force=yes"], tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr


# --- #135: what the installer writes, where, and what it never prints -----------

SECRET = "pa55-never-printed"


def _pki(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("server.pem", "client.pem", "client_key.pem"):
        (root / name).write_text("placeholder\n", encoding="utf-8")
    return Path(os.path.realpath(root))


@pytest.mark.parametrize("impl", ["python", "node"])
def test_codex_install_writes_config_toml_under_home(impl, tmp_path):
    """Codex reads ~/.codex/config.toml. The install must land there — under the
    scratch HOME, never the developer's — and leave the rest of the file alone."""
    target = tmp_path / ".codex" / "config.toml"
    target.parent.mkdir(parents=True)
    existing = 'model = "o3"\n\n[mcp_servers.other]\ncommand = "other"\n'
    target.write_text(existing, encoding="utf-8")

    proc = _run(impl, ["--install", "codex", "--url", URL], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Restart Codex" in proc.stdout

    written = target.read_text(encoding="utf-8")
    assert written.startswith(existing.rstrip("\n")), "other tables were rewritten"
    assert "[mcp_servers.opcua]" in written
    assert f'OPCUA_SERVER_URL = "{URL}"' in written
    if sys.version_info >= (3, 11):
        import tomllib

        parsed = tomllib.loads(written)
        assert parsed["model"] == "o3"
        assert parsed["mcp_servers"]["other"] == {"command": "other"}
        assert parsed["mcp_servers"]["opcua"]["env"]["OPCUA_PROFILE"] == "observe"
    assert list(tmp_path.glob(".codex/config.toml.bak-*")), "no backup was taken"

    again = _run(impl, ["--install", "codex"], tmp_path)
    assert again.returncode == 1
    assert "already configured" in again.stderr

    other_url = "opc.tcp://localhost:4841"
    forced = _run(impl, ["--install", "codex", "--force", "--url", other_url], tmp_path)
    assert forced.returncode == 0, forced.stderr
    rewritten = target.read_text(encoding="utf-8")
    assert rewritten.count("[mcp_servers.opcua]") == 1
    assert other_url in rewritten and URL not in rewritten


@pytest.mark.parametrize("impl", ["python", "node"])
def test_codex_home_is_honoured(impl, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    proc = _run(impl, ["--install", "codex", "--dry-run"], tmp_path, {"CODEX_HOME": str(elsewhere)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == f"Would write {elsewhere / 'config.toml'}:"


@pytest.mark.parametrize("impl", ["python", "node"])
def test_a_stored_password_reaches_only_the_file(impl, tmp_path):
    """The password is read from the environment, never from argv; it is never
    printed — not in the preview, the report, the summary or a warning — and the
    file that does hold it is readable by its owner only."""
    args = ["--install", "claude-desktop", "--username", "op", "--store-password-in-config"]
    env = {"OPCUA_PASSWORD": SECRET}

    preview = _run(impl, [*args, "--dry-run"], tmp_path, env)
    assert preview.returncode == 0, preview.stderr
    assert SECRET not in preview.stdout + preview.stderr
    assert "<redacted>" in preview.stdout
    assert "WARNING [password-stored-in-config]" in preview.stderr

    target = _config_target(impl, tmp_path)
    proc = _run(impl, args, tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert SECRET not in proc.stdout + proc.stderr

    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["opcua"]
    assert entry["env"]["OPCUA_PASSWORD"] == SECRET
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("impl", ["python", "node"])
def test_the_preview_hides_other_servers_credentials(impl, tmp_path):
    """A dry run prints the whole file, which holds other servers' tokens too.
    We cannot tell which values are secret, so none of theirs is shown."""
    target = _config_target(impl, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    other = {"command": "gh", "env": {"GITHUB_TOKEN": "ghp_" + "x" * 20}, "headers": {"A": "b"}}
    target.write_text(json.dumps({"mcpServers": {"github": other}}), encoding="utf-8")

    config = _dry_run_config(impl, tmp_path)
    assert config["mcpServers"]["github"]["env"] == {"GITHUB_TOKEN": "<redacted>"}
    assert config["mcpServers"]["github"]["headers"] == {"A": "<redacted>"}
    assert config["mcpServers"]["github"]["command"] == "gh"
    # ...and the real write keeps them intact.
    assert _run(impl, ["--install", "claude-desktop"], tmp_path).returncode == 0
    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["github"] == other


def test_both_runtimes_print_the_same_security_summary(tmp_path):
    """The summary and warnings are what a user decides on, so they are held to
    the same text, not only the same codes."""
    pki = _pki(tmp_path / "pki")
    args = [
        "--install", "codex", "--dry-run",
        "--url", "opc.tcp://plc.example:4840",
        "--security-policy", "Basic256Sha256",
        "--client-cert", "client.pem",
        "--client-key", "client_key.pem",
        "--username", "op",
        "--profile", "operator",
        "--allowed-write-nodes", "ns=2;i=5",
        "--allow-unverified-remote-control",
    ]  # fmt: skip
    python = _run("python", args, tmp_path / "py", cwd=pki)
    node = _run("node", args, tmp_path / "node", cwd=pki)
    assert python.returncode == node.returncode == 0, python.stderr + node.stderr
    assert python.stderr == node.stderr
    assert "NOT verified" in python.stderr


@pytest.mark.parametrize("impl", ["python", "node"])
def test_every_installer_setting_has_a_flag_and_no_secret_does(impl, tmp_path):
    """The flags come from contract/config.json, not from a list in the code."""
    helped = _run(impl, ["--help"], tmp_path).stdout
    for setting in load_config_schema()["settings"]:
        flag = "--" + setting["key"].replace("_", "-")
        if setting["secret"]:
            assert flag not in helped, f"{impl} offers {flag}"
        elif "installer" in setting["surfaces"]:
            assert flag in helped, f"{impl} has no {flag}"
