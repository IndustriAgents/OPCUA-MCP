"""``opcua-mcp-server --install <client>`` — write the MCP client config.

The audience for this server is automation engineers, not Python developers, and
"edit this JSON file, whose path differs per OS" is where they stall. This module
resolves the path, merges an entry into whatever is already there, and writes it
back atomically with a backup.

It is also the easiest way to configure the server, so it must be able to say
everything a secure deployment needs (#135). Its setting flags are therefore built
from ``/contract/config.json`` rather than listed here, the configuration it writes
is run through the server's own startup parsers before anything is written, and a
control profile aimed at a remote endpoint nobody verifies is refused unless the
user overrides that explicitly.

The Node runtime ships the same subcommand with the same flags, refusals and
warnings; ``tests/fixtures/install-cases.json`` is the table both are held to.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SERVER_URL
from .contract import load_config_schema
from .version import package_version

#: The key this server is registered under in the client config.
SERVER_KEY = "opcua"

#: Client identifiers accepted by ``--install``.
CLIENTS = ("claude-desktop", "codex")

#: Stands in for a sensitive value in anything printed.
REDACTED = "<redacted>"

#: The one secret the installer ever handles, and only from the environment.
_PASSWORD_ENV = "OPCUA_PASSWORD"


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


def codex_config_path(env: dict[str, str] | None = None, home: Path | None = None) -> Path:
    """Where Codex keeps its config: ``$CODEX_HOME/config.toml``, else ``~/.codex/``."""
    env = os.environ if env is None else env  # type: ignore[assignment]
    home = Path.home() if home is None else home
    return Path(env.get("CODEX_HOME") or str(home / ".codex")) / "config.toml"


def client_config_path(client: str) -> Path | None:
    """The config file ``client`` reads on this machine, or None if it has none here."""
    return codex_config_path() if client == "codex" else claude_desktop_config_path()


def _client_name(client: str) -> str:
    return "Codex" if client == "codex" else "Claude Desktop"


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
    executable: str,
    env: dict[str, str],
    prefix: str | None = None,
    frozen: bool | None = None,
) -> dict[str, Any]:
    """Build the entry that launches this server with ``env``.

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
    env = dict(env)
    if frozen:
        return {"command": executable, "env": env}
    if is_ephemeral_install(prefix):
        return {"command": "uvx", "args": ["opcua-mcp-server"], "env": env}
    return {"command": executable, "args": ["-m", "opcua_mcp_server"], "env": env}


class InstallRefusal(ValueError):
    """Why an install was refused. ``exit_code`` 2 is a usage error, 1 everything else."""

    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _already_configured() -> InstallRefusal:
    return InstallRefusal(
        "already-configured",
        f'an MCP server named "{SERVER_KEY}" is already configured — pass --force to replace it',
    )


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
        raise InstallRefusal(
            "unreadable-config",
            "`mcpServers` in the config file is not an object — refusing to overwrite it",
        )

    servers = dict(existing or {})
    replaced = SERVER_KEY in servers
    if replaced and not force:
        raise _already_configured()

    servers[SERVER_KEY] = entry
    base["mcpServers"] = servers
    return base, replaced


# --- Codex: TOML -----------------------------------------------------------------
#
# Codex keeps its MCP servers in TOML, and neither runtime ships a TOML writer
# (``tomllib`` only reads, and only from 3.11). The installer does not need one: it
# owns exactly one table, ``[mcp_servers.opcua]`` and its subtables, so it removes
# those and appends its own, leaving every other byte of the user's file as it
# was. What it cannot safely rewrite — the entry spelled as dotted keys or an
# inline table — it refuses rather than guesses at.


def toml_string(value: str) -> str:
    """A TOML basic string. Escapes the same characters in both runtimes."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def codex_block(entry: dict[str, Any]) -> str:
    """The ``[mcp_servers.opcua]`` block for ``entry``."""
    lines = [f"[mcp_servers.{SERVER_KEY}]", f"command = {toml_string(entry['command'])}"]
    if entry.get("args"):
        lines.append(f"args = {_toml_array(entry['args'])}")
    if entry.get("env_vars"):
        lines.append(f"env_vars = {_toml_array(entry['env_vars'])}")
    lines += ["", f"[mcp_servers.{SERVER_KEY}.env]"]
    lines += [f"{name} = {toml_string(value)}" for name, value in entry["env"].items()]
    return "\n".join(lines) + "\n"


_TOML_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]*?)\s*\]\]?\s*(#.*)?$")
_TOML_ASSIGNMENT = re.compile(r"""^\s*([A-Za-z0-9_."'-]+?)\s*=""")


def _toml_header_key(line: str) -> str | None:
    """A table header's dotted key with quoting and spacing normalised away."""
    match = _TOML_HEADER.match(line)
    if match is None:
        return None
    return ".".join(
        re.sub(r"""^(["'])(.*)\1$""", r"\2", part.strip()) for part in match.group(1).split(".")
    )


def merge_codex_config(
    text: str, entry: dict[str, Any], *, force: bool = False
) -> tuple[str, bool]:
    """Replace or append this server's block in a Codex ``config.toml``."""
    ours = f"mcp_servers.{SERVER_KEY}"
    kept: list[str] = []
    replaced = False
    in_ours = False
    table = ""
    for line in re.split(r"\r?\n", text):
        key = _toml_header_key(line)
        if key is not None:
            table = key
            in_ours = key == ours or key.startswith(f"{ours}.")
            if in_ours:
                replaced = True
        elif not in_ours:
            # The same server spelled some other way — `opcua = {...}` under
            # [mcp_servers], or `mcp_servers.opcua.command = ...` at the top
            # level. Rewriting either correctly needs a real TOML parser;
            # refusing does not.
            match = _TOML_ASSIGNMENT.match(line)
            if match is not None:
                dotted = (f"{table}." if table else "") + re.sub(r"[\"']", "", match.group(1))
                if dotted == ours or dotted.startswith(f"{ours}."):
                    raise InstallRefusal(
                        "unreadable-config",
                        f'the "{SERVER_KEY}" server in this file is written as dotted keys or an '
                        f"inline table, which --install does not rewrite — remove it by hand, "
                        f"then retry",
                    )
        if not in_ours:
            kept.append(line)
    if replaced and not force:
        raise _already_configured()

    while kept and kept[-1].strip() == "":
        kept.pop()
    head = "\n".join(kept) + "\n\n" if kept else ""
    return head + codex_block(entry), replaced


# --- the settings the flags are built from ---------------------------------------


def installer_settings() -> list[dict[str, Any]]:
    """Every setting ``--install`` offers a flag for, in schema order."""
    # `secret` is excluded here as well as by the schema test: a flag's value is
    # a process argument, which any local user can read.
    return [
        s
        for s in load_config_schema()["settings"]
        if "installer" in s["surfaces"] and not s["secret"]
    ]


def flag_for(setting: dict[str, Any]) -> str:
    """A setting's ``--flag``: its stable key with dashes."""
    return "--" + setting["key"].replace("_", "-")


@dataclass
class InstallOptions:
    """Parsed ``--install`` invocation."""

    client: str
    #: Raw flag values keyed by schema key; a boolean flag's value is "true".
    settings: dict[str, str] = field(default_factory=dict)
    force: bool = False
    dry_run: bool = False
    #: Write $OPCUA_PASSWORD into the client config in plain text.
    store_password_in_config: bool = False
    #: Write a control profile for a remote endpoint with no pinned certificate.
    allow_unverified_remote_control: bool = False


@dataclass
class Finding:
    """One thing a user should know about the config before relying on it."""

    code: str
    message: str


@dataclass
class InstallPlan:
    """A validated configuration, ready to merge into a client config."""

    #: The environment the entry carries, in schema order.
    env: dict[str, str]
    #: Variables the client passes through from its own environment (Codex).
    env_vars: list[str]
    summary: list[str]
    warnings: list[Finding]


def endpoint_host(url: str) -> str:
    """The host of an ``opc.tcp://host:port/path`` URL, lowercased; "" if none."""
    after_scheme = url[url.index("://") + 3 :] if "://" in url else ""
    authority = re.split(r"[/?#]", after_scheme)[0]
    authority = authority[authority.rfind("@") + 1 :]
    if authority.startswith("["):
        return authority[1 : authority.find("]")].lower()
    return authority.split(":")[0].lower()


def is_loopback_endpoint(url: str) -> bool:
    """Whether ``url`` names this machine. Anything unparseable counts as remote."""
    host = endpoint_host(url)
    return (
        host == "localhost"
        or host.endswith(".localhost")
        or re.fullmatch(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}", host) is not None
        or host in ("::1", "0:0:0:0:0:0:0:1")
    )


def _normalise(setting: dict[str, Any], raw: str, cwd: str) -> str:
    """One flag value, checked and normalised as its schema entry says."""
    flag = flag_for(setting)
    value = raw.strip()
    kind = setting["type"]
    if kind == "boolean":
        return "true"
    if value == "":
        raise InstallRefusal("invalid-value", f"{flag} needs a value", 2)

    if kind == "enum":
        lower = value.lower()
        aliases = setting.get("choiceAliases") or {}
        alias = next((a for a in aliases if a.lower() == lower), None)
        if alias is not None:
            return aliases[alias]
        canonical = next((c for c in setting.get("choices", []) if c.lower() == lower), None)
        if canonical is None:
            accepted = [*setting.get("choices", []), *aliases]
            raise InstallRefusal(
                "invalid-value", f'{flag}: "{value}" is not one of: {", ".join(accepted)}', 2
            )
        return canonical
    if kind == "number":
        try:
            number = float(value)
        except ValueError:
            number = math.nan
        minimum = setting.get("minimum", -math.inf)
        if not math.isfinite(number) or number < minimum:
            raise InstallRefusal(
                "invalid-value", f'{flag} must be a number >= {minimum}, got "{value}"', 2
            )
        return value
    if kind == "list":
        items = [item.strip() for item in value.split(",") if item.strip()]
        if not items:
            raise InstallRefusal("invalid-value", f"{flag} is empty", 2)
        return ",".join(items)
    if kind == "path":
        # A path flag is where somebody pastes a key instead of naming its file.
        # Refused without echoing it: the value may be the secret itself.
        if "-----BEGIN" in value or "\n" in value or "\r" in value:
            raise InstallRefusal(
                "key-material-on-command-line",
                f"{flag} takes the path to a file, not its contents — never paste key material "
                f"into a command line",
                2,
            )
        # Absolute, always: the client launches the server from a working
        # directory nobody chose, so a relative path would resolve somewhere else.
        path = os.path.abspath(os.path.join(cwd, value))
        shown = "(path not shown: it names a private key)" if setting["sensitive"] else path
        if setting.get("mustExist"):
            if not os.path.isfile(path):
                raise InstallRefusal("file-missing", f"{flag}: no such file {shown}")
        elif not os.path.isdir(os.path.dirname(path)):
            raise InstallRefusal(
                "file-missing",
                f"{flag}: the directory {os.path.dirname(path)} does not exist, so the server "
                f"could not create the file",
            )
        return path
    return value


def plan_install(options: InstallOptions, env: dict[str, str], cwd: str) -> InstallPlan:
    """Build, validate and assess the configuration an install would write.

    Pure apart from reading the files the flags name: it never opens an OPC UA
    connection and never touches the client config. Raises ``InstallRefusal``.
    """
    # Late: these pull in python-opcua and cryptography, which `--help` and
    # `--version` have no use for.
    from .config import parse_reconnect_config
    from .contract import CONTRACT
    from .policy import ToolPolicy, parse_policy_config
    from .security import parse_security_config

    settings = load_config_schema()["settings"]
    values: dict[str, str] = {}
    for setting in installer_settings():
        raw = options.settings.get(setting["key"])
        if raw is not None:
            values[setting["env"]] = _normalise(setting, raw, cwd)
    # Written even when it is the default: a config that says `observe` says so
    # to whoever reads it next, and cannot be widened by a policy file's profile.
    values.setdefault("OPCUA_PROFILE", "observe")

    # --- the password: from the environment, never from argv ----------------------
    username = values.get("OPCUA_USERNAME")
    env_vars: list[str] = []
    if options.store_password_in_config:
        if username is None:
            raise InstallRefusal("invalid-value", "--store-password-in-config needs --username", 2)
        password = env.get(_PASSWORD_ENV)
        if not password:
            raise InstallRefusal(
                "password-not-in-environment",
                f"--store-password-in-config copies {_PASSWORD_ENV} from this shell's "
                f"environment, and it is not set",
            )
        values[_PASSWORD_ENV] = password
    elif username is not None:
        if options.client == "codex":
            # Codex forwards a named variable from its own environment, so the
            # password never has to be written anywhere by us.
            env_vars.append(_PASSWORD_ENV)
        else:
            raise InstallRefusal(
                "password-needs-storage",
                f"{_client_name(options.client)} has no way to pass a password to a "
                f"hand-registered server except writing it into its config file. Use the "
                f".mcpb bundle, which keeps it in the OS keychain; or X.509 user login "
                f"(--user-cert, --user-key); or export {_PASSWORD_ENV} and pass "
                f"--store-password-in-config to write it in plain text",
            )

    config_env = {s["env"]: values[s["env"]] for s in settings if s["env"] in values}

    # --- the server's own verdict ---------------------------------------------------
    # The same parsers the server calls at startup, so a config it would refuse
    # is refused here instead, before it is written. A password Codex will pass
    # through is present at launch, so it stands in for one here.
    probe = dict(config_env)
    if _PASSWORD_ENV in env_vars:
        probe[_PASSWORD_ENV] = "supplied-at-launch"
    file_profile: str | None = None
    try:
        security = parse_security_config(probe)
        policy = ToolPolicy(parse_policy_config(probe))
        parse_reconnect_config(probe)
        if "OPCUA_POLICY_FILE" in config_env and "profile" not in options.settings:
            file_profile = parse_policy_config({**probe, "OPCUA_PROFILE": ""}).profile
    except Exception as error:
        raise InstallRefusal(
            "invalid-config", f"the server would refuse this configuration at startup: {error}"
        ) from error

    config = policy.config
    url = config_env["OPCUA_SERVER_URL"]
    loopback = is_loopback_endpoint(url)
    pinned = security.server_cert is not None
    control = config.profile != "observe"
    control_tools = sorted(
        t["name"]
        for t in CONTRACT["tools"]
        if t["accessClass"] in ("control", "alarm-action") and policy.is_visible(t)
    )

    # --- the one refusal that is about safety rather than validity ------------------
    if control and not loopback and not pinned and not options.allow_unverified_remote_control:
        raise InstallRefusal(
            "unverified-remote-control",
            f"profile={config.profile} would give control of {endpoint_host(url) or url}, a "
            f"remote endpoint whose identity nothing verifies. Pin its certificate with "
            f"--server-cert (with --security-policy, --client-cert and --client-key), or pass "
            f"--allow-unverified-remote-control to write it anyway on a lab network",
        )

    warnings: list[Finding] = []

    def warn(code: str, message: str) -> None:
        warnings.append(Finding(code, message))

    if control and not loopback and not pinned:
        warn(
            "unverified-remote-control",
            f"writing profile={config.profile} for a remote endpoint whose identity is not "
            f"verified, because --allow-unverified-remote-control was given",
        )
    if not loopback and security.policy == "None":
        warn(
            "no-channel-security",
            "the endpoint is remote and the channel has no security policy: every value read "
            "or written crosses the network unencrypted, to whoever answers. Set "
            "--security-policy",
        )
    elif not loopback and not pinned:
        warn(
            "server-not-pinned",
            "the channel is encrypted to whichever server answers at this address. Pin the "
            "server's certificate with --server-cert",
        )
    if security.username is not None and security.policy == "None":
        warn(
            "password-over-unencrypted-channel",
            "the password crosses an unencrypted channel unless the server's user-token "
            "policy protects it. Set --security-policy",
        )
    if _PASSWORD_ENV in config_env:
        warn(
            "password-stored-in-config",
            "the password is written in plain text into the client config file, readable by "
            "anything that can read that file",
        )
    if config.allow_insecure_control:
        warn(
            "insecure-control-override",
            "control tools are allowed over an unsecured channel "
            "(OPCUA_ALLOW_INSECURE_CONTROL). Never use this against production equipment",
        )
    if config.allow_out_of_range_writes:
        warn(
            "out-of-range-writes",
            "writes outside the node's published EURange are allowed "
            "(OPCUA_ALLOW_OUT_OF_RANGE_WRITES)",
        )
    if config.profile == "full":
        warn(
            "full-profile",
            "profile=full offers every tool with no allowlist; use it only with a tightly "
            "scoped OPC UA account",
        )
    if control and "OPCUA_AUDIT_FILE" not in config_env:
        warn(
            "control-without-audit-file",
            "control calls are audited to stderr only, which the client may not keep. Set "
            "--audit-file",
        )
    if control and not control_tools:
        warn(
            "no-control-tools",
            f"profile={config.profile} offers no control tool with these settings: set an "
            f"allowlist (--allowed-write-nodes, --allowed-methods, --allow-acknowledge-alarms "
            f"or a --policy-file) and a secured channel",
        )
    if file_profile is not None and file_profile != config.profile:
        warn(
            "policy-file-profile-overridden",
            f"the policy file asks for profile={file_profile}, but a control profile must be "
            f"chosen with --profile, so this config pins profile={config.profile}",
        )

    if security.user_cert is not None:
        user = "X.509 certificate"
    elif security.username is None:
        user = "anonymous"
    else:
        where = (
            f"password from ${_PASSWORD_ENV} when the client starts the server"
            if _PASSWORD_ENV in env_vars
            else "password stored in the config file"
        )
        user = f'"{security.username}", {where}'
    if security.policy == "None":
        channel = "None: unencrypted and unsigned"
        identity = "not verified (no channel security)"
    else:
        channel = f"{security.policy}, {security.mode}"
        identity = "verified: pinned certificate" if pinned else "NOT verified: no --server-cert"
    read_only = " (read-only)" if config.profile == "observe" else ""
    summary = [
        "Security summary:",
        f"  endpoint         {url} ({'this machine' if loopback else 'remote'})",
        f"  channel          {channel}",
        f"  server identity  {identity}",
        f"  user             {user}",
        f"  profile          {config.profile}{read_only}",
        f"  control tools    {', '.join(control_tools) if control_tools else 'none'}",
        f"  policy file      {config_env.get('OPCUA_POLICY_FILE', 'none')}",
        f"  audit trail      {config_env.get('OPCUA_AUDIT_FILE', 'stderr only')}",
    ]
    return InstallPlan(config_env, env_vars, summary, warnings)


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with every sensitive value replaced, for printing."""
    sensitive = {
        s["env"] for s in load_config_schema()["settings"] if s["sensitive"] or s["secret"]
    }
    return {name: REDACTED if name in sensitive else value for name, value in env.items()}


def _redact_json_config(config: dict[str, Any]) -> dict[str, Any]:
    """A JSON client config fit to print: ours redacted, other servers' values hidden.

    Other servers' ``env`` and ``headers`` routinely carry API tokens; we cannot
    tell which, and a preview is not a reason to put any of them on a screen or in
    a log.
    """
    servers: dict[str, Any] = {}
    for name, server in (config.get("mcpServers") or {}).items():
        if name == SERVER_KEY:
            servers[name] = {**server, "env": redact_env(server["env"])}
            continue
        if not isinstance(server, dict):
            servers[name] = server
            continue
        copy = dict(server)
        for key in ("env", "headers"):
            if isinstance(copy.get(key), dict):
                copy[key] = dict.fromkeys(copy[key], REDACTED)
        servers[name] = copy
    return {**config, "mcpServers": servers}


# --- files ----------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """A client config file's text, or "" when there is none.

    The encoding is explicit because ``Path.read_text`` otherwise defaults to the
    system locale, which on Windows is typically not UTF-8. Claude's config is
    UTF-8, and a Windows user whose profile path contains a non-ASCII character —
    hardly exotic — would otherwise get mojibake written back or an uncaught
    UnicodeDecodeError. The Node runtime has always passed "utf8" here.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _parse_json_config(path: Path, raw: str) -> Any:
    """Parse a JSON client config, tolerating absence but not corruption.

    An unparseable config is a hard error: overwriting it would silently destroy
    every other MCP server the user has configured.
    """
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise InstallRefusal(
            "unreadable-config", f"{path} is not valid JSON ({err}) — fix or move it, then retry"
        ) from err


def _write_text(path: Path, text: str, secret: bool) -> Path | None:
    """Write ``text`` to ``path`` atomically, backing up anything already there.

    ``secret`` makes the file readable by its owner only, from the moment it is
    created. Otherwise an existing file keeps its mode: a user who had already
    locked their config down must not find it loosened by an install.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    mode = 0o600 if secret else 0o666
    backup: Path | None = None
    if path.exists():
        if not secret:
            mode = path.stat().st_mode & 0o777
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copyfile(path, backup)

    # Write-then-rename: a crash mid-write must not leave a truncated config that
    # takes every other MCP server down with it. UTF-8 for the same reason as the
    # read.
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)
    return backup


def run_install(
    options: InstallOptions,
    *,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    executable: str | None = None,
) -> int:
    """Run ``--install``, reporting what it did. Returns a process exit code.

    The report goes to stdout; the security summary, warnings and errors to
    stderr.
    """
    env = dict(os.environ) if env is None else env
    path = config_path if config_path is not None else client_config_path(options.client)
    name = _client_name(options.client)
    try:
        if path is None:
            raise InstallRefusal(
                "no-config-location", f"{name} has no known config location on {sys.platform}."
            )
        plan = plan_install(options, env, os.getcwd() if cwd is None else cwd)
        entry = server_entry(sys.executable if executable is None else executable, plan.env)
        redacted = {**entry, "env": redact_env(entry["env"])}
        if plan.env_vars:
            entry["env_vars"] = list(plan.env_vars)
            redacted["env_vars"] = list(plan.env_vars)

        existing = _read_text(path)
        if options.client == "codex":
            text, replaced = merge_codex_config(existing, entry, force=options.force)
            preview = (
                "# Only this server's tables are shown; the rest of the file is left unchanged.\n"
                + codex_block(redacted).rstrip()
            )
        else:
            merged, replaced = merge_server_entry(
                _parse_json_config(path, existing), entry, force=options.force
            )
            # `ensure_ascii=False` so a non-ASCII value that came out of the
            # existing config goes back in as itself rather than as an escape.
            text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
            preview = json.dumps(_redact_json_config(merged), indent=2, ensure_ascii=False)
    except InstallRefusal as refusal:
        print(f"Error [{refusal.code}]: {refusal}", file=sys.stderr)
        return refusal.exit_code

    if options.dry_run:
        print(f"Would write {path}:")
        print(preview)
    else:
        backup = _write_text(path, text, _PASSWORD_ENV in plan.env)
        print(f'{"Replaced" if replaced else "Added"} MCP server "{SERVER_KEY}" in {path}')
        if backup:
            print(f"Previous config backed up to {backup}")
        print(f"Restart {name} for the change to take effect.")
    sys.stdout.flush()
    for line in plan.summary:
        print(line, file=sys.stderr)
    for warning in plan.warnings:
        print(f"WARNING [{warning.code}]: {warning.message}", file=sys.stderr)
    return 0


def _secret_flag_message(flag: str, env: str) -> str:
    """Refusal for a secret passed as a flag. Never echoes the value."""
    return (
        f"{flag} is not accepted: anything on a command line is visible to every local user "
        f"and lands in shell history. Leave the password out and see docs/install.md, or "
        f"export {env} and pass --store-password-in-config"
    )


def _build_parser() -> argparse.ArgumentParser:
    # `allow_abbrev=False`: argparse would otherwise take `--allow-insecure` for
    # `--allow-insecure-control`, which the Node parser (rightly) does not.
    parser = argparse.ArgumentParser(
        prog="opcua-mcp-server",
        description=(
            "MCP server for OPC UA. With no arguments it runs the server on stdio, "
            "which is how MCP clients invoke it."
        ),
        epilog=(
            "Passwords are never accepted as flags. See docs/install.md for how to supply one. "
            "opcua-mcp-server --verify-audit FILE [FILE ...] [--key-file KEY] checks the "
            "hash chain of an OPCUA_AUDIT_FILE (rotated files oldest first)."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=package_version(),
        help="print the version and exit",
    )
    install = parser.add_argument_group("install options")
    install.add_argument(
        "--install",
        metavar="CLIENT",
        choices=CLIENTS,
        help="register this server with an MCP client (choices: %(choices)s)",
    )
    install.add_argument(
        "--url", dest="setting_server_url", metavar="ENDPOINT", help="same as --server-url"
    )
    install.add_argument(
        "--force", action="store_true", help=f'replace an existing "{SERVER_KEY}" entry'
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="validate, print a redacted preview, write nothing",
    )
    install.add_argument(
        "--store-password-in-config",
        action="store_true",
        help="copy $OPCUA_PASSWORD into the config file (plain text)",
    )
    install.add_argument(
        "--allow-unverified-remote-control",
        action="store_true",
        help="write operator/full for a remote endpoint with no pinned --server-cert (lab only)",
    )

    schema = load_config_schema()
    groups: dict[str, argparse._ArgumentGroup] = {}
    for setting in installer_settings():
        category = setting["category"]
        if category not in groups:
            title = schema["categories"].get(category, {}).get("title", category)
            groups[category] = parser.add_argument_group(title)
        dest = f"setting_{setting['key']}"
        if setting["type"] == "boolean":
            groups[category].add_argument(
                flag_for(setting),
                dest=dest,
                action="store_const",
                const="true",
                help=setting["env"],
            )
        else:
            groups[category].add_argument(
                flag_for(setting), dest=dest, metavar=setting["type"].upper(), help=setting["env"]
            )

    for setting in schema["settings"]:
        if not setting["secret"]:
            continue
        message = _secret_flag_message(flag_for(setting), setting["env"])

        class _Refuse(argparse.Action):
            def __call__(self, parser, namespace, values, option_string=None, _msg=message):
                parser.error(_msg)

        parser.add_argument(flag_for(setting), nargs="?", action=_Refuse, help=argparse.SUPPRESS)
    return parser


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

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.install is None:
        parser.error("nothing to do — pass --install CLIENT or --help")
    settings = {
        name.removeprefix("setting_"): value
        for name, value in vars(args).items()
        if name.startswith("setting_") and value is not None
    }
    settings.setdefault("server_url", SERVER_URL)
    return run_install(
        InstallOptions(
            client=args.install,
            settings=settings,
            force=args.force,
            dry_run=args.dry_run,
            store_password_in_config=args.store_password_in_config,
            allow_unverified_remote_control=args.allow_unverified_remote_control,
        )
    )
