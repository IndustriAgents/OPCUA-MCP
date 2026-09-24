"""``opcua-mcp-server --install claude-desktop`` — write the MCP client config.

The audience for this server is automation engineers, not Python developers, and
"edit this JSON file, whose path differs per OS" is where they stall. This module
resolves the path, merges an entry into whatever is already there, and writes it
back atomically with a backup.

The Node runtime ships the same subcommand with the same flags and produces the
same config entry; ``tests/unit/test_install_parity.py`` holds the two to it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SERVER_URL
from .version import package_version

#: The key this server is registered under in ``mcpServers``.
SERVER_KEY = "opcua"

#: Client identifiers accepted by ``--install``.
CLIENTS = ("claude-desktop",)


def claude_desktop_config_path(
    platform: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    """Where Claude Desktop keeps its MCP server config, per platform.

    Returns None on a platform Claude Desktop does not ship for, so the caller can
    fail with an explanation rather than writing a file nobody will read.
    """
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env  # type: ignore[assignment]
    home = Path.home() if home is None else home

    if platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if platform == "win32":
        appdata = env.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if platform.startswith("linux"):
        # Claude Desktop is unofficial on Linux, but the community builds follow
        # the XDG base directory spec, so honour XDG_CONFIG_HOME when it is set.
        config_home = env.get("XDG_CONFIG_HOME") or str(home / ".config")
        return Path(config_home) / "Claude" / "claude_desktop_config.json"
    return None


# Path segments that mark a package-runner's throwaway environment. A server run
# from one of these is deleted the moment the cache is pruned, so writing its
# path into a config file produces an entry that works today and breaks later.
# `uvx` builds these under the uv cache; `pipx run` uses its own temp venvs.
_EPHEMERAL_PREFIXES = ("archive-v", "environments-v", ".tmp")


def is_ephemeral_install(prefix: str) -> bool:
    """True when ``prefix`` is a ``uvx``/``pipx run`` throwaway env, not a real install."""
    parts = Path(prefix).parts
    if any(part.startswith(_EPHEMERAL_PREFIXES) for part in parts):
        return True
    # pipx's ephemeral venvs live under its cache dir rather than its venvs dir.
    return "pipx" in parts and ".cache" in parts


def server_entry(
    executable: str, url: str, prefix: str | None = None, frozen: bool | None = None
) -> dict[str, Any]:
    """Build the ``mcpServers`` entry that launches this server.

    Three shapes, and which one you get matters:

    * **The executable alone**, in the PyInstaller build, where ``sys.executable``
      is this program rather than a Python that runs it. Handing that binary
      ``-m opcua_mcp_server`` would just make it fail to parse its own arguments.
    * **Absolute paths** (``/path/to/python -m opcua_mcp_server``) whenever this
      process is running from a real install. Desktop apps are launched from the
      GUI, not a login shell, so they inherit a bare ``PATH`` — a bare ``python``
      or ``uvx`` command is the single most common reason an MCP server that works
      in the terminal fails to start in Claude Desktop. Naming the interpreter
      outright sidesteps both ``PATH`` lookup and which virtualenv is active.
    * **``uvx opcua-mcp-server``** when this process was itself launched by
      ``uvx``, because the absolute path in that case points into a cache that
      will be pruned out from under the config.
    """
    prefix = sys.prefix if prefix is None else prefix
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    env = {"OPCUA_SERVER_URL": url}
    if frozen:
        return {"command": executable, "env": env}
    if is_ephemeral_install(prefix):
        return {"command": "uvx", "args": ["opcua-mcp-server"], "env": env}
    return {"command": executable, "args": ["-m", "opcua_mcp_server"], "env": env}


def merge_server_entry(
    config: Any, entry: dict[str, Any], *, force: bool = False
) -> tuple[dict[str, Any], bool]:
    """Merge ``entry`` into a client config under ``mcpServers[SERVER_KEY]``.

    Returns the new config and whether an existing entry was replaced. Other MCP
    servers in the file are left exactly as they were — this is somebody's working
    config, not ours to normalise.

    Raises ValueError when ``mcpServers`` exists but is not an object, rather than
    silently discarding it.
    """
    base = dict(config) if isinstance(config, dict) else {}

    existing = base.get("mcpServers")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError(
            "`mcpServers` in the config file is not an object — refusing to overwrite it"
        )

    servers = dict(existing or {})
    replaced = SERVER_KEY in servers
    if replaced and not force:
        raise ValueError(
            f'an MCP server named "{SERVER_KEY}" is already configured — pass --force to replace it'
        )

    servers[SERVER_KEY] = entry
    base["mcpServers"] = servers
    return base, replaced


def _read_config(path: Path) -> Any:
    """Read a client config file, tolerating absence but not corruption.

    An unparseable config is a hard error: overwriting it would silently destroy
    every other MCP server the user has configured.

    The encoding is explicit because ``Path.read_text`` otherwise defaults to the
    system locale, which on Windows is typically not UTF-8. Claude's config is
    UTF-8, and a Windows user whose profile path contains a non-ASCII character —
    hardly exotic — would otherwise get mojibake written back or an uncaught
    UnicodeDecodeError. The Node runtime has always passed "utf8" here.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(f"{path} is not valid JSON ({err}) — fix or move it, then retry") from err


def _write_config(path: Path, config: Any) -> Path | None:
    """Write ``config`` to ``path`` atomically, backing up anything already there."""
    path.parent.mkdir(parents=True, exist_ok=True)

    backup: Path | None = None
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copyfile(path, backup)

    # Write-then-rename: a crash mid-write must not leave a truncated config that
    # takes every other MCP server down with it.
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    # UTF-8 for the same reason as the read, and `ensure_ascii=False` so a
    # non-ASCII value that came out of the existing config goes back in as
    # itself rather than as an escape sequence.
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-mcp-server",
        description=(
            "MCP server for OPC UA. With no arguments it runs the server on stdio, "
            "which is how MCP clients invoke it."
        ),
        epilog=(
            "opcua-mcp-server --verify-audit FILE [FILE ...] [--key-file KEY] checks the "
            "hash chain of an OPCUA_AUDIT_FILE (rotated files oldest first)."
        ),
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=package_version(),
        help="print the version and exit",
    )
    parser.add_argument(
        "--install",
        metavar="CLIENT",
        choices=CLIENTS,
        help="register this server with an MCP client (choices: %(choices)s)",
    )
    parser.add_argument(
        "--url",
        default=SERVER_URL,
        metavar="ENDPOINT",
        help="OPC UA endpoint to record (default: $OPCUA_SERVER_URL, else %(default)s)",
    )
    parser.add_argument(
        "--force", action="store_true", help=f'replace an existing "{SERVER_KEY}" entry'
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the resulting config instead of writing it"
    )
    return parser


def run_install(
    url: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> int:
    """Run ``--install``, reporting what it did. Returns a process exit code."""
    path = config_path if config_path is not None else claude_desktop_config_path()
    if path is None:
        print(f"Claude Desktop has no known config location on {sys.platform}.")
        return 1

    entry = server_entry(sys.executable, url)

    try:
        merged, replaced = merge_server_entry(_read_config(path), entry, force=force)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Would write {path}:")
        print(json.dumps(merged, indent=2))
        return 0

    backup = _write_config(path, merged)
    print(f'{"Replaced" if replaced else "Added"} MCP server "{SERVER_KEY}" in {path}')
    if backup:
        print(f"Previous config backed up to {backup}")
    print(f"OPC UA endpoint: {url}")
    print("Restart Claude Desktop for the change to take effect.")
    return 0


def dispatch(argv: list[str] | None = None) -> int | None:
    """Handle CLI arguments.

    Returns an exit code when the arguments asked for something other than
    serving, and None when the caller should start the MCP server — which is the
    no-argument case, and how every MCP client invokes us.
    """
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return None
    if argv[0] == "--verify-audit":
        # Its own grammar (files, then an optional key), so it is routed before
        # argparse rather than taught to it. Needs no OPC UA server.
        from .audit import run_verify

        return run_verify(argv[1:])

    args = _build_parser().parse_args(argv)
    if args.install is None:
        _build_parser().error("nothing to do — pass --install CLIENT or --help")
    return run_install(args.url, force=args.force, dry_run=args.dry_run)
