"""Result files: one JSON document per conformance run, safe to publish.

A result records *what was run and what happened*, never *what the plant
said*: the server's own BuildInfo (product, manufacturer, version), what its
endpoints offer, the runtime and package versions, timestamps, and for each
scenario an outcome with a short detail made of status names, type names and
counts. No endpoint, node id, value, username or password is written — the
scenarios are built so there is nothing of the kind to write, and
`assert_publishable` checks the finished document anyway.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from conformance.config import Config
from conformance.runtime import ROOT
from conformance.scenarios import SCENARIOS, Outcome

RESULT_SCHEMA_VERSION = 1
RESULTS_DIR = ROOT / "compatibility" / "results"

#: Every outcome a published scenario result may carry.
OUTCOMES = (
    "pass",
    "project-bug",
    "library-limitation",
    "server-limitation",
    "unsupported-optional",
    "not-configured",
    "untriaged-failure",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(config: Config, scenario: str, runtime: str, outcome: Outcome) -> dict[str, Any]:
    """Turn a scenario's raw outcome into a published one.

    `unsupported` becomes unsupported-optional: the server said so itself. A
    failure takes the classification a human gave it in the config, or stays
    an untriaged failure that `check` will refuse to publish. A triage entry
    whose failure has gone is reported, not silently kept.
    """
    entry: dict[str, Any] = {"id": scenario, "detail": outcome.detail}
    if outcome.evidence:
        entry["evidence"] = outcome.evidence
    triage = config.triage_for(scenario, runtime)
    if outcome.result == "pass":
        entry["outcome"] = "pass"
        if triage:
            entry["stale_triage"] = triage.summary
    elif outcome.result == "unsupported":
        entry["outcome"] = "unsupported-optional"
    elif outcome.result == "not-configured":
        entry["outcome"] = "not-configured"
    elif triage:
        entry["outcome"] = triage.outcome
        entry["triage"] = {"summary": triage.summary}
        if triage.tracking:
            entry["triage"]["tracking"] = triage.tracking
    else:
        entry["outcome"] = "untriaged-failure"
    return entry


def result_path(server_id: str, when: str) -> Path:
    return RESULTS_DIR / f"{server_id}-{when[:10]}.json"


# Things that must never appear in a published result, whatever a scenario did.
_FORBIDDEN = (
    (re.compile(r"opc\.tcp://"), "an endpoint URL"),
    (re.compile(r"\bnsu=|\bns=\d+;[isgb]="), "a node id"),
    (re.compile(r"-----BEGIN"), "key or certificate material"),
)


MIN_CHECKED_SECRET = 6


def assert_publishable(document: dict[str, Any], secrets: list[str] = ()) -> None:
    text = json.dumps(document)
    for pattern, what in _FORBIDDEN:
        match = pattern.search(text)
        if match:
            # The surrounding JSON says which field leaked, without the leak.
            where = text[max(0, match.start() - 60) : match.start()]
            raise ValueError(f"result contains {what} (after …{where!r}); refusing to write it")
    for secret in secrets:
        # A credential as short as a demo server's "user" cannot be told from
        # the document's own words, so the backstop skips it; tool output was
        # already redacted of every credential, whatever its length.
        if len(secret or "") < MIN_CHECKED_SECRET:
            continue
        if re.search(rf"(?<![\w-]){re.escape(secret)}(?![\w-])", text):
            raise ValueError("result contains a credential; refusing to write it")


def validate(document: dict[str, Any]) -> list[str]:
    """Structural problems with a result document, empty if it is well formed."""
    problems = []
    if document.get("schemaVersion") != RESULT_SCHEMA_VERSION:
        problems.append("schemaVersion")
    server = document.get("server", {})
    for key in ("id", "name", "implementation", "build_info"):
        if key not in server:
            problems.append(f"server.{key} missing")
    known = {s.id for s in SCENARIOS}
    for run in document.get("runs", []):
        for key in ("runtime", "package_version", "started", "finished", "scenarios"):
            if key not in run:
                problems.append(f"run.{key} missing")
        for scenario in run.get("scenarios", []):
            if scenario.get("id") not in known:
                problems.append(f"unknown scenario {scenario.get('id')!r}")
            if scenario.get("outcome") not in OUTCOMES:
                problems.append(f"{scenario.get('id')}: outcome {scenario.get('outcome')!r}")
            classified = scenario.get("outcome") in (
                "project-bug",
                "library-limitation",
                "server-limitation",
            )
            if classified and not scenario.get("triage", {}).get("summary"):
                problems.append(f"{scenario.get('id')}: classified without a triage summary")
    return problems


def load_all(directory: Path = RESULTS_DIR) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


def latest_per_server(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest result for each server id — the matrix shows current evidence only."""
    newest: dict[str, dict[str, Any]] = {}
    for document in documents:
        server_id = document["server"]["id"]
        if server_id not in newest or document["finished"] > newest[server_id]["finished"]:
            newest[server_id] = document
    return [newest[key] for key in sorted(newest)]
