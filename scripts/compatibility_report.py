"""Release evidence for Model A, built only from successful required-suite runs.

The report describes repository/mock coverage; dated independent-server results
are included separately and never promoted into evidence for the new commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_NAME = "runtime-compatibility.json"
GROUPS = {
    "unit",
    "core (main mock)",
    "runtime: python",
    "runtime: node",
    "security",
    "history",
    "aggregates",
    "events",
    "alarms & conditions",
    "resilience",
    "differential parity",
}
MATRIX = {("3.10", "22"), ("3.13", "22"), ("3.13", "24")}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def successful(counts: dict) -> bool:
    return (
        isinstance(counts.get("passed"), int)
        and not isinstance(counts["passed"], bool)
        and counts["passed"] > 0
        and all(value == 0 for key, value in counts.items() if key != "passed")
    )


def node_evidence(path: Path) -> dict:
    # The Node built-in JUnit reporter emits nested suites; count testcases,
    # rather than summing parent suite attributes and counting them twice.
    text = path.read_text(encoding="utf-8")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("JUnit entities are not permitted")
    cases = list(ET.fromstring(text).iter("testcase"))
    if not cases or any(
        case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")
    ):
        raise ValueError("Node unit evidence is empty, failed or skipped")
    return {"passed": len(cases), "sha256": digest(path)}


def check_pytest(evidence: dict) -> None:
    if (
        evidence.get("schemaVersion") != 1
        or evidence.get("required") is not True
        or evidence.get("exitStatus") != 0
        or evidence.get("problems")
        or not successful(evidence.get("outcomes", {}))
        or not GROUPS.issubset(evidence.get("groups", {}))
        or not all(successful(evidence["groups"][group]) for group in GROUPS)
    ):
        raise ValueError("Python/E2E evidence must be a complete successful required-mode run")
    if evidence.get("pythonBackend") not in {"legacy", "asyncua"}:
        raise ValueError("Python backend must identify the selected client")
    if not re.fullmatch(r"\d+\.\d+\.\d+", evidence.get("pythonVersion", "")):
        raise ValueError("Python version must be an exact version")


def build_run(pytest_path: Path, node_path: Path, commit: str, node_version: str) -> dict:
    evidence = read_json(pytest_path)
    check_pytest(evidence)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source commit must be a full Git commit hash")
    if not re.fullmatch(r"\d+\.\d+\.\d+", node_version):
        raise ValueError("Node version must be an exact version")
    return {
        "sourceCommit": commit,
        "pythonVersion": evidence["pythonVersion"],
        "pythonBackend": evidence["pythonBackend"],
        "nodeVersion": node_version,
        "pytest": {**evidence, "sha256": digest(pytest_path)},
        "nodeUnit": node_evidence(node_path),
    }


def release_report(runs: list[dict], *, require_matrix: bool = False) -> dict:
    if not runs or len({run["sourceCommit"] for run in runs}) != 1:
        raise ValueError("all evidence must describe one source commit")
    if len({run.get("pythonBackend") for run in runs}) != 1:
        raise ValueError("all evidence must describe one Python backend")
    for run in runs:
        check_pytest(run["pytest"])
        if not re.fullmatch(r"[0-9a-f]{40}", run["sourceCommit"]):
            raise ValueError("source commit must be a full Git commit hash")
        if not re.fullmatch(r"\d+\.\d+\.\d+", run["nodeVersion"]):
            raise ValueError("Node version must be an exact version")
        if run.get("pythonBackend") != run["pytest"].get("pythonBackend"):
            raise ValueError("Python backend differs from the test evidence")
        if run["pythonVersion"] != run["pytest"]["pythonVersion"]:
            raise ValueError("runtime version differs from the test evidence")
        if not successful({"passed": run["nodeUnit"].get("passed")}):
            raise ValueError("Node unit evidence is empty")
    legs = {
        (run["pythonVersion"].rsplit(".", 1)[0], run["nodeVersion"].split(".", 1)[0])
        for run in runs
    }
    if len(legs) != len(runs) or (require_matrix and legs != MATRIX):
        raise ValueError(
            "release evidence must cover each required runtime matrix leg exactly once"
        )
    paths = [
        ROOT / "contract" / name
        for name in ("tools.json", "config.json", "runtime-differences.json")
    ]
    fixtures = sorted((ROOT / "tests/fixtures").glob("*.json"))
    lockfiles = [ROOT / "uv.lock", ROOT / "packages/server-node/package-lock.json"]
    independent = [
        {
            "file": path.relative_to(ROOT).as_posix(),
            "sha256": digest(path),
            "report": read_json(path),
        }
        for path in sorted((ROOT / "compatibility/results").glob("*.json"))
    ]
    return {
        "schemaVersion": 1,
        "supportModel": "two-first-class-runtimes",
        "packageVersion": read_json(ROOT / "packages/server-node/package.json")["version"],
        "sourceCommit": runs[0]["sourceCommit"],
        "pythonBackend": runs[0]["pythonBackend"],
        "qualification": "repository-mocks; independent results are dated historical evidence",
        "matrixComplete": legs == MATRIX,
        "runs": sorted(runs, key=lambda run: (run["pythonVersion"], run["nodeVersion"])),
        "inputs": {p.relative_to(ROOT).as_posix(): digest(p) for p in paths + fixtures + lockfiles},
        "declaredRuntimeDifferences": read_json(ROOT / "contract/runtime-differences.json"),
        "independentServerResults": independent,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pytest", type=Path)
    parser.add_argument("--node-junit", type=Path)
    parser.add_argument("--merge", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if args.merge:
        reports = [read_json(path) for path in args.merge]
        # The inputs and declarations must be identical, not only the SHA.
        if any(report["inputs"] != reports[0]["inputs"] for report in reports):
            raise ValueError("matrix reports have different contract/fixture inputs")
        result = release_report(
            [run for report in reports for run in report["runs"]], require_matrix=True
        )
        if result["sourceCommit"] != commit:
            raise ValueError("aggregation checkout differs from the tested commit")
        if result["inputs"] != reports[0]["inputs"]:
            raise ValueError("aggregation checkout differs from the tested input digests")
    elif args.pytest and args.node_junit:
        node_version = (
            subprocess.check_output(["node", "--version"], text=True).strip().removeprefix("v")
        )
        result = release_report([build_run(args.pytest, args.node_junit, commit, node_version)])
    else:
        parser.error("provide --pytest and --node-junit, or --merge")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
