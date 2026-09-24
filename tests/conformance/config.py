"""The conformance config file: which server, how to reach it, what to exercise.

A config describes one server *deployment*, not one product: vendor address
spaces differ, so the node map says which of its nodes stand in for "a Double",
"a writable node", "a method" and so on. A scenario whose node is not mapped is
recorded as not configured rather than failed, which is what lets the same
harness run against a PLC with no history and a historian with no methods.

Two rules are enforced here rather than trusted to reviewers, because a config
file is exactly the kind of thing that gets pasted into an issue:

* **No secret is ever a literal.** Usernames, passwords and anything else that
  identifies an account are ``{"env": "VAR"}`` references, resolved from the
  environment at run time. A literal is a load error, so a config cannot be
  committed or shared with a password in it.
* **Certificates and keys are paths, never material.** A PEM block in a config
  is refused.

Nothing from the config beyond the server's identity, the security settings and
the scenario outcomes reaches a result file — no endpoint, no node id, no value.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Keys whose value names or proves an identity. Each must be an env reference.
SECRET_KEY = re.compile(r"(password|passwd|secret|token|passphrase|username)", re.I)

#: The outcomes a triage entry may assign. `pass` and `not-configured` are
#: decided by the harness, never by a human.
TRIAGE_OUTCOMES = (
    "project-bug",
    "library-limitation",
    "server-limitation",
    "unsupported-optional",
)

SECURITY_MODES = ("None", "Sign", "SignAndEncrypt")
RUNTIMES = ("python", "node")


class ConfigError(ValueError):
    """The config file is unusable; the message says which key and why."""


def _is_env_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) <= {"env", "default"} and "env" in value


def _check_no_literal_secrets(node: Any, path: str) -> None:
    if isinstance(node, dict):
        if _is_env_ref(node):
            return
        for key, value in node.items():
            where = f"{path}.{key}" if path else key
            if SECRET_KEY.search(key) and not _is_env_ref(value) and value is not None:
                raise ConfigError(
                    f"{where}: credentials must be an environment reference "
                    '({"env": "VARIABLE"}), never a literal in the config file'
                )
            _check_no_literal_secrets(value, where)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_no_literal_secrets(value, f"{path}[{index}]")
    elif isinstance(node, str) and "-----BEGIN" in node:
        raise ConfigError(f"{path}: key or certificate material in the config; give a path")


def resolve(value: Any, *, required: bool = True) -> str | None:
    """A literal string, or the environment variable an env reference names."""
    if value is None:
        return None
    if _is_env_ref(value):
        resolved = os.environ.get(value["env"], value.get("default"))
        if resolved is None and required:
            raise ConfigError(f"environment variable {value['env']} is not set")
        return resolved
    if isinstance(value, str):
        return value
    raise ConfigError(f"expected a string or an env reference, got {value!r}")


@dataclass
class Triage:
    """A human's classification of a failure, carried in the config.

    The harness can tell pass from fail; it cannot tell a bug in this project
    from a limitation of the client library or of the server. Someone has to
    look, and the answer belongs next to the config it was found with, so a
    rerun applies it — and says so when the failure it explained has gone.
    """

    scenario: str
    runtimes: tuple[str, ...]
    outcome: str
    summary: str
    tracking: str | None = None


@dataclass
class Config:
    path: Path
    raw: dict[str, Any]
    server: dict[str, Any]
    endpoint: Any
    lab: dict[str, Any] | None
    security: dict[str, Any]
    identities: dict[str, Any]
    namespaces: dict[str, str]
    nodes: dict[str, Any]
    triage: list[Triage] = field(default_factory=list)

    @property
    def server_id(self) -> str:
        return self.server["id"]

    def endpoint_url(self) -> str:
        return resolve(self.endpoint)

    def node(self, key: str) -> Any:
        """A node-map entry by dotted key (``scalars.Double``), or None."""
        current: Any = self.nodes
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def triage_for(self, scenario: str, runtime: str) -> Triage | None:
        for entry in self.triage:
            if entry.scenario == scenario and runtime in entry.runtimes:
                return entry
        return None


def _require(data: dict, key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return data[key]


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path}: not valid JSON: {error}") from error
    return parse(raw, path)


def parse(raw: dict[str, Any], path: Path = Path("<config>")) -> Config:
    if raw.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigError(f"schemaVersion must be {SCHEMA_VERSION}")
    _check_no_literal_secrets(raw, "")

    server = _require(raw, "server", "config")
    for key in ("id", "name", "implementation"):
        _require(server, key, "server")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", server["id"]):
        raise ConfigError("server.id: lowercase letters, digits and hyphens only")

    security = raw.get("security", {})
    for mode in security.get("modes", []):
        if mode not in SECURITY_MODES:
            raise ConfigError(f"security.modes: {mode!r} is not one of {SECURITY_MODES}")
    default = security.get("default", {"mode": "None"})
    if default.get("mode", "None") not in SECURITY_MODES:
        raise ConfigError("security.default.mode is not a security mode")
    if security.get("clientTrust", "unknown") not in ("enforced", "trust-all", "unknown"):
        raise ConfigError("security.clientTrust must be enforced, trust-all or unknown")

    triage = []
    for index, entry in enumerate(raw.get("triage", [])):
        where = f"triage[{index}]"
        outcome = _require(entry, "outcome", where)
        if outcome not in TRIAGE_OUTCOMES:
            raise ConfigError(f"{where}.outcome must be one of {TRIAGE_OUTCOMES}")
        runtimes = tuple(entry.get("runtimes", RUNTIMES))
        if not set(runtimes) <= set(RUNTIMES):
            raise ConfigError(f"{where}.runtimes must name python and/or node")
        triage.append(
            Triage(
                scenario=_require(entry, "scenario", where),
                runtimes=runtimes,
                outcome=outcome,
                summary=_require(entry, "summary", where),
                tracking=entry.get("tracking"),
            )
        )

    return Config(
        path=path,
        raw=raw,
        server=server,
        endpoint=_require(raw, "endpoint", "config"),
        lab=raw.get("lab"),
        security=security,
        identities=raw.get("identities", {"anonymous": True}),
        namespaces=raw.get("namespaces", {}),
        nodes=raw.get("nodes", {}),
        triage=triage,
    )
