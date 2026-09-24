"""The shared tool contract — the single source of truth both servers derive from."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

#: Levels above this module to reach the repo root in a source checkout:
#: opcua_mcp_server → src → server-python → packages → <root>.
_REPO_ROOT_DEPTH = 4


def contract_candidates(module_file: Path, name: str = "tools.json") -> list[Path]:
    """Where to look for a contract file, in order, given this module's location.

    ``name`` is the file under ``/contract``: ``tools.json`` (the tool surface) by
    default, or ``config.json`` (the configuration schema). Both ship the same way.

    1. ``name`` inside this package — how it ships in the wheel (see the
       ``force-include`` in pyproject.toml) and in the frozen single-file build
       (see ``packaging/opcua-mcp-server.spec``). Bundling it *inside* the package
       rather than at the install root keeps the distribution from adding
       top-level files to ``site-packages``.
    2. ``/contract/<name>`` at the repo root — the canonical source, used when
       running from a checkout (dev, editable installs, tests).

    The second candidate is omitted when this module is not five levels deep,
    which is the case inside a frozen app: PyInstaller unpacks to something like
    ``/tmp/_MEIabc123/opcua_mcp_server/`` and there is no repo above it. Computing
    it unconditionally used to raise ``IndexError`` from ``Path.parents`` *while
    building the list*, so the bundled copy in (1) never got tried and the
    executable died on import — but only on Linux, where the unpack directory is
    shallow enough for the index to be out of range at all.
    """
    parents = module_file.parents
    candidates = [module_file.parent / name]
    if len(parents) > _REPO_ROOT_DEPTH:
        candidates.append(parents[_REPO_ROOT_DEPTH] / "contract" / name)
    return candidates


def _load(name: str, what: str) -> dict:
    candidates = contract_candidates(Path(__file__).resolve(), name)
    for path in candidates:
        if path.is_file():
            # Explicit UTF-8: `read_text` otherwise defaults to the system locale,
            # so a non-ASCII character in a tool description would make the
            # package unimportable on a Windows machine and nowhere else.
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"{what} not found; looked in " + ", ".join(str(p) for p in candidates))


def load_contract() -> dict:
    """Load the shared tool contract (single source of truth).

    Without a copy bundled inside the package, a pip/uvx install would raise
    FileNotFoundError on import, because the repo-root path does not exist outside
    a checkout. See :func:`contract_candidates` for the search order.
    """
    return _load("tools.json", "Shared tool contract")


@lru_cache(maxsize=1)
def load_config_schema() -> dict:
    """Every setting this server reads, from the canonical schema (#133).

    Loaded on first use rather than at import: the server's own startup does not
    need it, so a fault in it must not be able to stop the server starting. It
    exists for code that runs from an installed package and has to describe the
    configuration surface — ``--install`` (#135) — without a second hand-kept
    list. Cached, so treat the result as read-only.
    """
    return _load("config.json", "Configuration schema")


# Shared tool contract so tool descriptions and capability node IDs stay in sync
# with the Node server.
CONTRACT = load_contract()
DESC = {t["name"]: t["description"] for t in CONTRACT["tools"]}
HISTORY_NODE_ID = CONTRACT["capabilities"]["history"]["nodeId"]
HISTORY_EVENTS_NODE_ID = CONTRACT["capabilities"]["historyEvents"]["nodeId"]
AGGREGATE_NODE_ID = CONTRACT["capabilities"]["aggregate"]["nodeId"]

#: The standard nodes `get_server_status` reads, from the contract for the same
#: reason the capability node IDs are: both servers must ask the same questions.
SERVER_STATUS_NODE_ID = CONTRACT["diagnostics"]["serverStatusNodeId"]
NAMESPACE_ARRAY_NODE_ID = CONTRACT["diagnostics"]["namespaceArrayNodeId"]

#: Alarms & Conditions wiring: the well-known node IDs, the event field list both
#: servers select on, and the defaults their tool descriptions promise.
EVENTS = CONTRACT["events"]

#: The resources both servers expose, keyed by URI, and the one this server
#: registers. Sourced from the contract for the same reason the descriptions are:
#: an MCP client that has learned one runtime's resource surface must find the
#: other's identical.
RESOURCES = {r["uri"]: r for r in CONTRACT["resources"]}
SUBSCRIPTIONS_RESOURCE = RESOURCES["opcua://subscriptions"]
