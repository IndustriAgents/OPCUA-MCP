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
    claude_desktop_config_path,
    is_ephemeral_install,
    merge_server_entry,
    run_install,
    server_entry,
)

URL = "opc.tcp://plc:4840"


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
    assert server_entry("/usr/bin/python3", URL, prefix="/srv/app/.venv", frozen=False) == {
        "command": "/usr/bin/python3",
        "args": ["-m", "opcua_mcp_server"],
        "env": {"OPCUA_SERVER_URL": URL},
    }


def test_a_frozen_build_names_only_itself():
    """The PyInstaller binary is not a Python: handing it ``-m opcua_mcp_server``
    would make it fail to parse its own arguments."""
    assert server_entry("/opt/opcua-mcp-server", URL, prefix="/whatever", frozen=True) == {
        "command": "/opt/opcua-mcp-server",
        "env": {"OPCUA_SERVER_URL": URL},
    }


def test_a_uvx_run_records_the_uvx_invocation_not_the_cache_path():
    """A uv cache path is correct today and pruned tomorrow."""
    assert server_entry("/c/uv/archive-v0/a/bin/python", URL, prefix="/c/uv/archive-v0/a") == {
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
    assert run_install(URL, config_path=target) == 0
    entry = json.loads(target.read_text())["mcpServers"][SERVER_KEY]
    assert entry["env"] == {"OPCUA_SERVER_URL": URL}


def test_dry_run_prints_the_result_and_writes_nothing(tmp_path, config_path, capsys):
    assert run_install(URL, dry_run=True, config_path=config_path) == 0
    assert not config_path.exists()
    assert URL in capsys.readouterr().out


def test_backs_up_an_existing_config_before_replacing_an_entry(tmp_path, config_path):
    before = {"mcpServers": {SERVER_KEY: {"command": "old"}, "other": {"command": "x"}}}
    config_path.write_text(json.dumps(before))

    assert run_install(URL, force=True, config_path=config_path) == 0

    backups = [p for p in tmp_path.iterdir() if ".bak-" in p.name]
    assert len(backups) == 1, "expected exactly one backup"
    assert json.loads(backups[0].read_text()) == before

    after = json.loads(config_path.read_text())
    assert after["mcpServers"][SERVER_KEY]["command"] != "old"
    assert after["mcpServers"]["other"] == {"command": "x"}


def test_an_existing_entry_without_force_fails_without_touching_the_file(config_path, capsys):
    before = json.dumps({"mcpServers": {SERVER_KEY: {"command": "old"}}})
    config_path.write_text(before)

    assert run_install(URL, config_path=config_path) == 1
    assert config_path.read_text() == before
    assert "already configured" in capsys.readouterr().err


def test_a_corrupt_config_is_an_error_not_something_to_overwrite(config_path, capsys):
    """Overwriting an unreadable config would silently destroy every other MCP
    server the user has set up, so it has to be a hard stop."""
    config_path.write_text("{ not json")

    assert run_install(URL, config_path=config_path) == 1
    assert config_path.read_text() == "{ not json"
    assert "not valid JSON" in capsys.readouterr().err


def test_an_empty_config_file_is_treated_as_an_empty_config(config_path):
    config_path.write_text("")
    assert run_install(URL, config_path=config_path) == 0
    assert json.loads(config_path.read_text())["mcpServers"][SERVER_KEY]


def test_every_advertised_client_is_a_string():
    """`--install` advertises these in its help; the Node runtime must offer the
    same set (see test_install_parity.py)."""
    assert CLIENTS and all(isinstance(c, str) for c in CLIENTS)
