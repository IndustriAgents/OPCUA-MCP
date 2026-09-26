"""Deprecation warnings fail the suite, except the upstream ones on the allowlist.

Issue #150. A deprecation warning is a dated notice that something will stop
working. Left to scroll past, it stops being read at all: python-opcua alone
emits one per OPC UA message on Python 3.12+, so a new one — from our own code,
or from a dependency bump — would arrive unnoticed under the volume, and be
found when the API is actually removed. So every deprecation is an error, and
the ones we cannot fix ourselves are listed, individually, in
`fixtures/deprecation-allowlist.json`, each with an issue, an owner and a
condition for removing it.

Two places a warning can surface, so two consumers of the one table:

- **In process** — the unit tests import both packages' modules, and the e2e
  tests drive python-opcua directly. `filterwarnings_lines()` turns the table into
  `filterwarnings` lines, installed by this module's own `pytest_configure`
  (it is a plugin: `conftest.py` loads it through `pytest_plugins`).
- **In a server's stderr** — the e2e tests run each MCP server as a subprocess,
  where pytest's filters cannot reach. `unexpected_deprecations()` reads what
  the server printed; `e2e/test_deprecations_e2e.py` drives both runtimes and
  fails on anything it returns.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ALLOWLIST_PATH = Path(__file__).resolve().parent / "fixtures" / "deprecation-allowlist.json"

# What an entry must say for the suite to accept it. An allowlist entry without
# an owner or an exit is how a temporary suppression becomes permanent.
REQUIRED_FIELDS = ("runtime", "package", "message", "issue", "owner", "remove_when")

# Python's default `warnings` output: `<path>:<line>: <Category>: <message>`.
_PYTHON_WARNING_LINE = re.compile(r"^(?P<path>.+?):\d+: (?P<category>\w+): (?P<message>.*)$")

# Any line saying "deprecat…" is a deprecation report until shown otherwise:
# node-opcua announces its own through a logger, not through process warnings,
# so the category alone would miss exactly the one #150 set out to fix.
_DEPRECATION = re.compile(r"deprecat", re.IGNORECASE)


def load_allowlist() -> list[dict[str, str]]:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))["entries"]


def filterwarnings_lines() -> list[str]:
    """`filterwarnings` lines: error on every deprecation, then the exceptions.

    pytest applies these in order with the last match winning, so the blanket
    `error` lines come first and each allowlisted `ignore` overrides them for
    its module and message only. The fields go in unescaped — both are regexes —
    which is why the unit tests forbid a `:` in either: it is the separator.
    """
    lines = ["error::DeprecationWarning", "error::PendingDeprecationWarning"]
    for entry in load_allowlist():
        if entry["runtime"] == "python":
            lines.append(f"ignore:{entry['message']}:DeprecationWarning:{entry['module']}")
    return lines


def pytest_configure(config) -> None:
    # Appended to the ini's own `filterwarnings`, so these are applied after —
    # and win over — anything configured there.
    for line in filterwarnings_lines():
        config.addinivalue_line("filterwarnings", line)


def module_of(path: str) -> str:
    """The dotted module name for a source path in a warning line.

    `…/site-packages/opcua/ua/uaprotocol_auto.py` is `opcua.ua.uaprotocol_auto`,
    and `…/src/opcua_mcp_server/server.py` (the editable install) is
    `opcua_mcp_server.server` — the same names pytest's module filter sees, so
    one allowlist regex serves both consumers.
    """
    parts = [part for part in re.split(r"[\\/]", path) if part]
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][: -len(".py")]
    for anchor in ("site-packages", "src"):
        if anchor in parts:
            start = len(parts) - parts[::-1].index(anchor)
            parts = parts[start:]
            break
    else:
        parts = parts[-1:]
    if len(parts) > 1 and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _allowed(line: str, runtime: str, entries: list[dict[str, str]]) -> bool:
    if runtime == "python":
        match = _PYTHON_WARNING_LINE.match(line.strip())
        # The in-process filters allowlist DeprecationWarning only, so this does too.
        if not match or match["category"] != "DeprecationWarning":
            return False
        module = module_of(match["path"])
        return any(
            re.match(entry["module"], module)
            and re.match(entry["message"], match["message"], re.IGNORECASE)
            for entry in entries
            if entry["runtime"] == "python"
        )
    return any(
        re.search(entry["message"], line, re.IGNORECASE)
        for entry in entries
        if entry["runtime"] == runtime
    )


def unexpected_deprecations(stderr: str, runtime: str) -> list[str]:
    """Every line of a server's stderr that reports a deprecation not allowlisted."""
    entries = load_allowlist()
    return [
        line
        for line in stderr.splitlines()
        if _DEPRECATION.search(line) and not _allowed(line, runtime, entries)
    ]
