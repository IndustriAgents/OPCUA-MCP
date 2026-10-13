"""A release must not claim parity from failed, partial or mixed-source evidence."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy

import pytest
from conftest import ROOT

_spec = importlib.util.spec_from_file_location(
    "compatibility_report", ROOT / "scripts/compatibility_report.py"
)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


@pytest.fixture
def evidence(tmp_path):
    document = {
        "schemaVersion": 1,
        "required": True,
        "pythonVersion": "3.13.3",
        "pythonBackend": "legacy",
        "exitStatus": 0,
        "outcomes": {"passed": 2500},
        "groups": {group: {"passed": 2} for group in report.GROUPS},
        "problems": [],
    }
    python = tmp_path / "pytest.json"
    node = tmp_path / "node.xml"
    python.write_text(json.dumps(document), encoding="utf-8")
    node.write_text(
        '<testsuites><testsuite tests="2"><testsuite tests="2">'
        '<testcase name="one"/><testcase name="two"/>'
        "</testsuite></testsuite></testsuites>",
        encoding="utf-8",
    )
    return document, python, node


def build(evidence):
    _, python, node = evidence
    return report.build_run(python, node, "a" * 40, "22.14.0")


def test_successful_evidence_counts_nested_node_suites_once(evidence):
    run = build(evidence)
    assert run["nodeUnit"]["passed"] == 2
    assert run["pytest"]["groups"]["runtime: python"]["passed"] == 2
    assert run["pytest"]["groups"]["runtime: node"]["passed"] == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc.update(required=False),
        lambda doc: doc.update(exitStatus=1),
        lambda doc: doc["outcomes"].update(failed=1),
        lambda doc: doc["outcomes"].update(skipped=1),
        lambda doc: doc["outcomes"].update({"not run": 1}),
        lambda doc: doc["groups"].pop("runtime: python"),
        lambda doc: doc["groups"].pop("runtime: node"),
        lambda doc: doc["groups"].update({"alarms & conditions": {"passed": 0}}),
        lambda doc: doc["groups"].update({"security": {"passed": 2, "skipped": 1}}),
        lambda doc: doc.update(problems=["missing subsystem"]),
    ],
)
def test_partial_python_or_e2e_evidence_cannot_be_published(evidence, change):
    document, python, _ = evidence
    change(document)
    python.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="complete successful required-mode"):
        build(evidence)


@pytest.mark.parametrize("element", ["failure", "error", "skipped"])
def test_failed_or_skipped_node_unit_evidence_cannot_be_published(evidence, element):
    _, _, node = evidence
    node.write_text(f"<testsuite><testcase><{element}/></testcase></testsuite>", encoding="utf-8")
    with pytest.raises(ValueError, match="empty, failed or skipped"):
        build(evidence)


def test_empty_node_unit_evidence_cannot_be_published(evidence):
    _, _, node = evidence
    node.write_text("<testsuite/>", encoding="utf-8")
    with pytest.raises(ValueError, match="empty, failed or skipped"):
        build(evidence)


def matrix(run):
    legs = []
    for python, node in sorted(report.MATRIX):
        leg = deepcopy(run)
        leg["pythonVersion"] = python + ".1"
        leg["pytest"]["pythonVersion"] = leg["pythonVersion"]
        leg["nodeVersion"] = node + ".14.0"
        legs.append(leg)
    return legs


def test_a_complete_matrix_is_deterministic_and_keeps_historical_evidence_separate(evidence):
    runs = matrix(build(evidence))
    result = report.release_report(runs, require_matrix=True)
    assert result == report.release_report(list(reversed(runs)), require_matrix=True)
    assert result["matrixComplete"] is True
    assert result["declaredRuntimeDifferences"] == report.read_json(
        ROOT / "contract/runtime-differences.json"
    )
    assert result["independentServerResults"]
    assert "historical evidence" in result["qualification"]


def test_missing_matrix_leg_blocks_release(evidence):
    with pytest.raises(ValueError, match="each required runtime matrix leg"):
        report.release_report(matrix(build(evidence))[:-1], require_matrix=True)


def test_duplicate_matrix_leg_blocks_release(evidence):
    runs = matrix(build(evidence))
    with pytest.raises(ValueError, match="each required runtime matrix leg"):
        report.release_report([*runs, runs[0]], require_matrix=True)


def test_mixed_source_commits_block_release(evidence):
    runs = matrix(build(evidence))
    runs[0]["sourceCommit"] = "b" * 40
    with pytest.raises(ValueError, match="one source commit"):
        report.release_report(runs, require_matrix=True)


def test_a_failed_matrix_report_is_rechecked_during_aggregation(evidence):
    runs = matrix(build(evidence))
    runs[0]["pytest"]["outcomes"]["failed"] = 1
    with pytest.raises(ValueError, match="complete successful required-mode"):
        report.release_report(runs, require_matrix=True)


def test_runtime_version_cannot_disagree_with_its_evidence(evidence):
    runs = matrix(build(evidence))
    runs[0]["pytest"]["pythonVersion"] = "3.13.3"
    with pytest.raises(ValueError, match="runtime version differs"):
        report.release_report(runs, require_matrix=True)


def test_workflow_matrices_match_the_release_report_requirement():
    import re

    for name in ("ci.yml", "publish.yml", "release.yml"):
        source = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        legs = re.findall(r'python-version: "([\d.]+)"\s+node-version: "([\d.]+)"', source)
        assert set(legs) == report.MATRIX, name


@pytest.mark.parametrize("backend", [None, "unknown"])
def test_unknown_or_missing_backend_cannot_claim_qualification(evidence, backend):
    document, python, _ = evidence
    document["pythonBackend"] = backend
    python.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="backend must identify"):
        build(evidence)


def test_mixed_backend_matrix_cannot_claim_release_qualification(evidence):
    runs = matrix(build(evidence))
    runs[0]["pythonBackend"] = "asyncua"
    runs[0]["pytest"]["pythonBackend"] = "asyncua"
    with pytest.raises(ValueError, match="one Python backend"):
        report.release_report(runs, require_matrix=True)


def test_backend_cannot_disagree_with_the_test_evidence(evidence):
    run = build(evidence)
    run["pytest"]["pythonBackend"] = "asyncua"
    with pytest.raises(ValueError, match="backend differs"):
        report.release_report([run])
