"""Unit tests for ``--install`` — argument handling, the config entry it writes,
and the merge into an existing client config.

No OPC UA server and no MCP transport: these exercise pure functions plus a temp
directory. The Node equivalents are in ``packages/server-node/test/install.test.mjs``,
and ``test_install_parity.py`` checks the two runtimes actually agree when run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opcua_mcp_server.install import (
    CLIENTS,
    SERVER_KEY,
    InstallOptions,
    claude_desktop_config_path,
    codex_block,
    codex_config_path,
    is_ephemeral_install,
    is_loopback_endpoint,
    merge_codex_config,
    merge_server_entry,
    run_install,
    server_entry,
    toml_string,
)

URL = "opc.tcp://plc:4840"
ENV = {"OPCUA_SERVER_URL": URL}


def install(config_path: Path, *, force: bool = False, dry_run: bool = False) -> int:
    """`--install claude-desktop --url URL` against ``config_path``.

    A loopback-or-not endpoint with the default profile: nothing here is about the
    security rules, which ``test_install_cases.py`` covers from the shared table.
    """
    options = InstallOptions("claude-desktop", {"server_url": URL}, force=force, dry_run=dry_run)
    return run_install(options, config_path=config_path, env={}, cwd=str(config_path.parent))


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "claude_desktop_config.json"


# --- config location ----------------------------------------------------------


def test_macos_uses_application_support():
    assert claude_desktop_config_path("darwin", {}, Path("/Users/e")) == Path(
        "/Users/e/Library/Application Support/Claude/claude_desktop_config.json"
    )


def test_windows_prefers_appdata_over_a_guessed_profile_path():
    assert claude_desktop_config_path("win32", {"APPDATA": "/R"}, Path("/U/e")) == Path(
        "/R/Claude/claude_desktop_config.json"
    )
    assert claude_desktop_config_path("win32", {}, Path("/U/e")) == Path(
        "/U/e/AppData/Roaming/Claude/claude_desktop_config.json"
    )


def test_linux_honours_xdg_config_home():
    assert claude_desktop_config_path("linux", {"XDG_CONFIG_HOME": "/x"}, Path("/home/e")) == Path(
        "/x/Claude/claude_desktop_config.json"
    )
    assert claude_desktop_config_path("linux", {}, Path("/home/e")) == Path(
        "/home/e/.config/Claude/claude_desktop_config.json"
    )


def test_unsupported_platform_has_no_path_rather_than_a_wrong_one():
    """Better to say "no known location on aix" than to write a file nobody reads."""
    assert claude_desktop_config_path("aix", {}, Path("/home/e")) is None


# --- which command to record --------------------------------------------------


def test_recognises_uvx_and_pipx_throwaway_environments():
    assert is_ephemeral_install("/home/e/.cache/uv/archive-v0/abc123")
    assert is_ephemeral_install("/home/e/.cache/uv/environments-v2/xyz")
    assert is_ephemeral_install("/home/e/.local/pipx/.cache/9f/venv")


def test_a_real_virtualenv_is_not_ephemeral():
    assert not is_ephemeral_install("/srv/app/.venv")
    assert not is_ephemeral_install("/home/e/.local/pipx/venvs/opcua-mcp-server")


def test_a_real_install_names_the_interpreter_absolutely():
    """Claude Desktop is launched from the GUI and inherits no login-shell PATH,
    so a bare ``python`` is the classic "works in my terminal, not in the app"."""
    assert server_entry("/usr/bin/python3", ENV, prefix="/srv/app/.venv", frozen=False) == {
        "command": "/usr/bin/python3",
        "args": ["-m", "opcua_mcp_server"],
        "env": {"OPCUA_SERVER_URL": URL},
    }


def test_a_frozen_build_names_only_itself():
    """The PyInstaller binary is not a Python: handing it ``-m opcua_mcp_server``
    would make it fail to parse its own arguments."""
    assert server_entry("/opt/opcua-mcp-server", ENV, prefix="/whatever", frozen=True) == {
        "command": "/opt/opcua-mcp-server",
        "env": {"OPCUA_SERVER_URL": URL},
    }


def test_a_uvx_run_records_the_uvx_invocation_not_the_cache_path():
    """A uv cache path is correct today and pruned tomorrow."""
    assert server_entry("/c/uv/archive-v0/a/bin/python", ENV, prefix="/c/uv/archive-v0/a") == {
        "command": "uvx",
        "args": ["opcua-mcp-server"],
        "env": {"OPCUA_SERVER_URL": URL},
    }


# --- merging into an existing config ------------------------------------------

ENTRY = {"command": "/p", "args": ["-m", "opcua_mcp_server"], "env": {"OPCUA_SERVER_URL": URL}}


def test_adds_to_an_empty_config():
    config, replaced = merge_server_entry({}, ENTRY)
    assert replaced is False
    assert config["mcpServers"][SERVER_KEY] == ENTRY


def test_leaves_other_servers_and_unrelated_keys_alone():
    """The user's other MCP servers are not ours to touch — the whole risk of
    writing to a config file we did not create."""
    existing = {"theme": "dark", "mcpServers": {"filesystem": {"command": "fs"}}}
    config, _ = merge_server_entry(existing, ENTRY)
    assert config["theme"] == "dark"
    assert config["mcpServers"]["filesystem"] == {"command": "fs"}


def test_refuses_to_replace_an_existing_entry_without_force():
    existing = {"mcpServers": {SERVER_KEY: {"command": "old"}}}
    with pytest.raises(ValueError, match="already configured"):
        merge_server_entry(existing, ENTRY)

    config, replaced = merge_server_entry(existing, ENTRY, force=True)
    assert replaced is True
    assert config["mcpServers"][SERVER_KEY] == ENTRY


def test_does_not_mutate_the_config_it_was_given():
    existing = {"mcpServers": {"filesystem": {"command": "fs"}}}
    merge_server_entry(existing, ENTRY)
    assert list(existing["mcpServers"]) == ["filesystem"]


def test_rejects_an_mcpservers_that_is_not_an_object():
    with pytest.raises(ValueError, match="not an object"):
        merge_server_entry({"mcpServers": []}, ENTRY)


# --- writing -------------------------------------------------------------------


def test_creates_the_config_file_and_its_parent_directory(tmp_path: Path):
    target = tmp_path / "Claude" / "claude_desktop_config.json"  # directory absent
    assert install(target) == 0
    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]
    assert entry["env"] == {"OPCUA_SERVER_URL": URL, "OPCUA_PROFILE": "observe"}


def test_dry_run_prints_the_result_and_writes_nothing(tmp_path, config_path, capsys):
    assert install(config_path, dry_run=True) == 0
    assert not config_path.exists()
    assert URL in capsys.readouterr().out


def test_backs_up_an_existing_config_before_replacing_an_entry(tmp_path, config_path):
    before = {"mcpServers": {SERVER_KEY: {"command": "old"}, "other": {"command": "x"}}}
    config_path.write_text(json.dumps(before))

    assert install(config_path, force=True) == 0

    backups = [p for p in tmp_path.iterdir() if ".bak-" in p.name]
    assert len(backups) == 1, "expected exactly one backup"
    assert json.loads(backups[0].read_text(encoding="utf-8")) == before

    after = json.loads(config_path.read_text(encoding="utf-8"))
    assert after["mcpServers"][SERVER_KEY]["command"] != "old"
    assert after["mcpServers"]["other"] == {"command": "x"}


def test_an_existing_entry_without_force_fails_without_touching_the_file(config_path, capsys):
    before = json.dumps({"mcpServers": {SERVER_KEY: {"command": "old"}}})
    config_path.write_text(before)

    assert install(config_path) == 1
    assert config_path.read_text(encoding="utf-8") == before
    assert "already configured" in capsys.readouterr().err


def test_a_corrupt_config_is_an_error_not_something_to_overwrite(config_path, capsys):
    """Overwriting an unreadable config would silently destroy every other MCP
    server the user has set up, so it has to be a hard stop."""
    config_path.write_text("{ not json")

    assert install(config_path) == 1
    assert config_path.read_text(encoding="utf-8") == "{ not json"
    assert "not valid JSON" in capsys.readouterr().err


def test_an_empty_config_file_is_treated_as_an_empty_config(config_path):
    config_path.write_text("")
    assert install(config_path) == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]


def test_every_advertised_client_is_a_string():
    """`--install` advertises these in its help; the Node runtime must offer the
    same set (see test_install_parity.py)."""
    assert CLIENTS and all(isinstance(c, str) for c in CLIENTS)


# --- encoding ------------------------------------------------------------------


def test_a_non_ascii_config_round_trips_unchanged(config_path):
    """Reading and rewriting must not corrupt non-ASCII values.

    `Path.read_text` defaults to the system locale, which on Windows is typically
    not UTF-8, while Claude's config is. A user whose profile path contains an
    accented character — `C:\\Users\\José` — would otherwise get mojibake written
    back over their config, or an uncaught UnicodeDecodeError. The Node runtime
    has always passed "utf8" explicitly, so this was a parity gap too.
    """
    existing = {"mcpServers": {"café": {"command": "/Users/José/bin/serveur", "args": ["—flag"]}}}
    config_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    assert install(config_path) == 0

    after = json.loads(config_path.read_text(encoding="utf-8"))
    assert after["mcpServers"]["café"] == existing["mcpServers"]["café"]


def test_the_written_config_is_utf8_bytes(config_path):
    """Not just round-trippable through our own reader — actually UTF-8 on disk,
    since Claude Desktop is the one that has to read it back."""
    config_path.write_text(json.dumps({"mcpServers": {"ré": {"command": "x"}}}), encoding="utf-8")
    assert install(config_path) == 0

    raw = config_path.read_bytes()
    assert "ré".encode() in raw, "non-ASCII key was not written back as UTF-8"


# --- Codex (#135) --------------------------------------------------------------

CODEX_ENTRY = {"command": "/p", "args": ["-m", "opcua_mcp_server"], "env": ENV}


def test_codex_lives_under_codex_home_else_the_home_directory(tmp_path):
    assert codex_config_path({}, tmp_path) == tmp_path / ".codex" / "config.toml"
    elsewhere = str(tmp_path / "x")
    assert codex_config_path({"CODEX_HOME": elsewhere}, tmp_path) == tmp_path / "x" / "config.toml"


def test_toml_strings_escape_what_toml_requires():
    """Windows paths are full of backslashes; a raw one is an invalid escape."""
    assert toml_string('C:\\Users\\"q"') == '"C:\\\\Users\\\\\\"q\\""'
    assert toml_string("a\nb\x7f") == '"a\\u000ab\\u007f"'
    assert toml_string("café") == '"café"'


def test_codex_merge_appends_and_leaves_the_rest_alone():
    existing = '# mine\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n\n'
    text, replaced = merge_codex_config(existing, CODEX_ENTRY)
    assert replaced is False
    assert text.startswith('# mine\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n')
    assert text.endswith(codex_block(CODEX_ENTRY))


def test_codex_merge_replaces_only_its_own_tables_and_only_with_force():
    existing = (
        '[mcp_servers."opcua"]\ncommand = "old"\n\n[mcp_servers.opcua.env]\nA = "1"\n\n'
        '[mcp_servers.other]\ncommand = "x"\n'
    )
    with pytest.raises(ValueError, match="already configured"):
        merge_codex_config(existing, CODEX_ENTRY)

    text, replaced = merge_codex_config(existing, CODEX_ENTRY, force=True)
    assert replaced is True
    assert "old" not in text and 'A = "1"' not in text
    assert '[mcp_servers.other]\ncommand = "x"' in text
    assert text.count("[mcp_servers.opcua]") == 1


@pytest.mark.parametrize(
    "existing",
    [
        '[mcp_servers]\nopcua = { command = "old" }\n',
        'mcp_servers.opcua.command = "old"\n',
    ],
)
def test_codex_merge_refuses_an_entry_it_cannot_rewrite(existing):
    """Rewriting dotted keys or an inline table needs a real TOML parser."""
    with pytest.raises(ValueError, match="does not rewrite"):
        merge_codex_config(existing, CODEX_ENTRY, force=True)


# --- endpoints (#135) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "loopback"),
    [
        ("opc.tcp://localhost:4840", True),
        ("opc.tcp://LOCALHOST:4840/path", True),
        ("opc.tcp://127.0.0.1:4840", True),
        ("opc.tcp://127.8.9.10", True),
        ("opc.tcp://[::1]:4840", True),
        ("opc.tcp://user@localhost:4840", True),
        ("opc.tcp://plc:4840", False),
        ("opc.tcp://localhost.example.com:4840", False),
        ("opc.tcp://127.0.0.1.example.com:4840", False),
        ("opc.tcp://10.0.0.1:4840", False),
        ("opc.tcp://[fe80::1]:4840", False),
        ("localhost:4840", False),  # no scheme: unparseable, so treated as remote
    ],
)
def test_loopback_is_recognised_and_everything_else_is_remote(url, loopback):
    assert is_loopback_endpoint(url) is loopback
