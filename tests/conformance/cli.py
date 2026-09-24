"""`python -m conformance run | render | check`."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from conformance import matrix, results
from conformance.config import RUNTIMES, Config, ConfigError, load
from conformance.lab import Discovery, LabServer, Pki, discover
from conformance.runtime import ROOT, available, describe
from conformance.scenarios import SCENARIOS, Context, failed

#: A scenario that has not finished in this long has hung, which is a finding.
SCENARIO_TIMEOUT = 240


def _git_commit() -> str:
    """The commit both runtimes ran from, marked `-dirty` if the tree was not."""
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--abbrev=7", "--exclude=*"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def _describe(error: BaseException) -> str:
    """The innermost error: an anyio task group wraps the one that matters."""
    while getattr(error, "exceptions", None):  # an (anyio) exception group
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"


async def _run_runtime(
    config: Config,
    runtime: str,
    pki: Pki,
    discovery: Discovery,
    lab: LabServer | None,
    only: set[str] | None,
) -> tuple[dict[str, Any], Context]:
    ctx = Context(config, runtime, pki, discovery, lab)
    ctx.log_dir = pki.directory.parent / "logs"
    ctx.log_dir.mkdir(exist_ok=True)
    run: dict[str, Any] = {**describe(runtime), "started": results.now_iso(), "scenarios": []}
    for scenario in SCENARIOS:
        if only and scenario.id not in only:
            continue
        began = time.monotonic()
        ctx.scenario = scenario.id
        try:
            outcome = await asyncio.wait_for(scenario.run(ctx), SCENARIO_TIMEOUT)
        except asyncio.TimeoutError:
            outcome = failed(f"did not finish within {SCENARIO_TIMEOUT} s")
        except Exception as error:
            outcome = failed(ctx.redact(_describe(error)))
        entry = results.classify(config, scenario.id, runtime, outcome)
        entry["duration_s"] = round(time.monotonic() - began, 1)
        run["scenarios"].append(entry)
        print(
            f"  {runtime:<6} {scenario.id:<28} {entry['outcome']:<22} {entry['detail'][:90]}",
            flush=True,
        )
        if entry["outcome"] == "untriaged-failure":
            print(f"         stderr: {ctx.log_dir / f'{runtime}-{scenario.id}.log'}", flush=True)
    run["finished"] = results.now_iso()
    run["summary"] = dict(Counter(s["outcome"] for s in run["scenarios"]))
    return run, ctx


def _configuration(config: Config) -> dict[str, Any]:
    """What the run was configured to exercise — settings, never values."""
    mapped = sorted(key for key, value in config.nodes.items() if value not in (None, [], {}))
    identities = [name for name in ("anonymous", "user", "x509") if config.identities.get(name)]
    security = config.security
    default = security.get("default", {})
    return {
        "security_modes": security.get("modes", []),
        "security_policy": security.get("policy", "Basic256Sha256"),
        "default_channel": f"{default.get('policy', 'None')}/{default.get('mode', 'None')}",
        "client_trust": security.get("clientTrust", "unknown"),
        "identities": identities,
        "lab_server_controlled": bool(config.lab),
        "node_map": mapped,
    }


async def _run(
    config: Config, runtimes: list[str], only: set[str] | None, keep_lab: bool, work: Path
) -> dict:
    lab_settings = config.lab or {}
    pki = Pki.generate(
        work / "pki",
        lab_settings.get("serverApplicationUri", "urn:opcua-mcp:conformance-lab:server"),
    )
    lab = None
    if config.lab and not keep_lab:
        state = work / "state"
        state.mkdir()
        lab = LabServer(config, pki, state)
        lab.prepare()
        lab.start()
        print(f"lab server started (log: {lab.log})", flush=True)
        if lab.warmup_seconds:
            await asyncio.sleep(lab.warmup_seconds)

    started = results.now_iso()
    try:
        discovery = discover(config.endpoint_url(), pki)
        if discovery.error:
            print(f"endpoint discovery failed: {discovery.error}", file=sys.stderr)
        runs, build_info = [], None
        for runtime in runtimes:
            reason = available(runtime)
            if reason:
                raise SystemExit(f"cannot run {runtime}: {reason}")
            run, ctx = await _run_runtime(config, runtime, pki, discovery, lab, only)
            runs.append(run)
            build_info = build_info or ctx.build_info
    finally:
        if lab:
            lab.stop()

    server = {
        key: config.server[key]
        for key in (
            "id",
            "name",
            "implementation",
            "language",
            "independent",
            "independenceNote",
            "source",
        )
        if key in config.server
    }
    server["build_info"] = build_info or {}
    return {
        "schemaVersion": results.RESULT_SCHEMA_VERSION,
        "server": server,
        "endpoint_features": {"endpoints": discovery.endpoints, "discovery_error": discovery.error},
        "configuration": _configuration(config),
        "harness": {
            "git_commit": _git_commit(),
            "config": str(config.path.resolve().relative_to(ROOT))
            if config.path.resolve().is_relative_to(ROOT)
            else config.path.name,
        },
        "started": started,
        "finished": results.now_iso(),
        "runs": runs,
    }


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load(args.config)
    except ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2
    runtimes = args.runtime or list(RUNTIMES)
    only = set(args.only) if args.only else None
    work = Path(tempfile.mkdtemp(prefix=f"conformance-{config.server_id}-"))
    document = asyncio.run(_run(config, runtimes, only, args.external, work))

    secrets = [
        value
        for value in (
            _env_value(config.identities.get("user", {}) or {}, "username"),
            _env_value(config.identities.get("user", {}) or {}, "password"),
        )
        if value
    ]
    try:
        results.assert_publishable(document, secrets)
    except ValueError as error:
        # Kept for whoever fixes the scenario that leaked, in the run's own
        # temporary directory — never where a result would be published.
        kept = work / "unpublishable-result.json"
        kept.write_text(json.dumps(document, indent=2), encoding="utf-8")
        print(f"{error}\n(kept in {kept})", file=sys.stderr)
        return 2
    problems = results.validate(document)
    if problems:
        print("result is malformed: " + "; ".join(problems), file=sys.stderr)
        return 2

    out = (
        Path(args.out) if args.out else results.result_path(config.server_id, document["finished"])
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    untriaged = 0
    for run in document["runs"]:
        print(
            f"{run['runtime']}: " + ", ".join(f"{v} {k}" for k, v in sorted(run["summary"].items()))
        )
        for scenario in run["scenarios"]:
            if scenario.get("stale_triage"):
                print(f"  note: {scenario['id']} now passes; its triage entry can go")
            untriaged += scenario["outcome"] == "untriaged-failure"
    if untriaged:
        print(
            f"{untriaged} untriaged failure(s): classify each in the config's `triage` "
            "before publishing (see docs/compatibility.md)",
            file=sys.stderr,
        )
        return 1
    return 0


def _env_value(block: dict, key: str) -> str | None:
    from conformance.config import resolve

    try:
        return resolve(block.get(key), required=False)
    except ConfigError:
        return None


def cmd_render(args: argparse.Namespace) -> int:
    changed = matrix.write(results.load_all())
    print("docs/compatibility.md updated" if changed else "docs/compatibility.md already current")
    return 0


def check() -> list[str]:
    """Why the published results or the docs are not in order; empty if they are."""
    problems = []
    documents = results.load_all()
    for document in documents:
        name = f"{document.get('server', {}).get('id')}-{document.get('finished', '')[:10]}"
        problems += [f"{name}: {p}" for p in results.validate(document)]
        try:
            results.assert_publishable(document)
        except ValueError as error:
            problems.append(f"{name}: {error}")
        for run in document.get("runs", []):
            for scenario in run.get("scenarios", []):
                if scenario.get("outcome") == "untriaged-failure":
                    problems.append(f"{name}: {run['runtime']} {scenario['id']} is untriaged")
    text = matrix.DOC.read_text(encoding="utf-8")
    if matrix.current_section(text) != matrix.render(documents):
        problems.append(
            "docs/compatibility.md matrix is stale — run "
            "`cd tests && uv run --no-sync python -m conformance render`"
        )
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    problems = check()
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m conformance", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the scenarios against the server a config describes")
    run.add_argument("--config", required=True, help="path to a conformance config (JSON)")
    run.add_argument("--runtime", action="append", choices=RUNTIMES, help="default: both")
    run.add_argument(
        "--only", action="append", metavar="SCENARIO", help="run only these scenario ids"
    )
    run.add_argument("--out", help="where to write the result (default: compatibility/results/)")
    run.add_argument(
        "--external",
        action="store_true",
        help="the server is already running: do not start the config's lab server "
        "(restart scenarios then report not configured)",
    )
    run.set_defaults(func=cmd_run)

    sub.add_parser("render", help="regenerate the matrix in docs/compatibility.md").set_defaults(
        func=cmd_render
    )
    sub.add_parser(
        "check", help="fail if results are unpublishable or the docs are stale"
    ).set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    return args.func(args)
