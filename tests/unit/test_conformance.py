"""The conformance harness's own rules (#147), and the published matrix.

The harness itself needs a real OPC UA server and is never part of the default
run. What *is* checked on every run: that a config cannot carry a credential,
that a result cannot carry an endpoint, node id or secret, that a failure is
never published unclassified, and that the matrix in docs/compatibility.md is
exactly what the committed result files say — so the compatibility claim is
generated from dated evidence and cannot be edited by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance import cli, matrix, results
from conformance.config import ConfigError, load, parse
from conformance.lab import Discovery
from conformance.scenarios import GROUPS, SCENARIOS, Context, Outcome

ROOT = Path(__file__).resolve().parents[2]
LAB_CONFIGS = sorted((ROOT / "compatibility" / "labs").glob("*.json"))


def minimal(**overrides) -> dict:
    config = {
        "schemaVersion": 1,
        "server": {"id": "example", "name": "Example", "implementation": "Example SDK"},
        "endpoint": "opc.tcp://127.0.0.1:4840",
    }
    config.update(overrides)
    return config


# --- config -----------------------------------------------------------------------


@pytest.mark.parametrize("path", LAB_CONFIGS, ids=lambda p: p.name)
def test_every_committed_lab_config_loads(path):
    config = load(path)
    unknown_triage = {t.scenario for t in config.triage} - {s.id for s in SCENARIOS}
    assert not unknown_triage, f"{path.name}: triage for unknown scenarios {unknown_triage}"


@pytest.mark.parametrize(
    "identities",
    [
        {"user": {"username": "operator", "password": {"env": "PW"}}},
        {"user": {"username": {"env": "USER"}, "password": "hunter2"}},
    ],
    ids=["literal username", "literal password"],
)
def test_a_literal_credential_is_a_load_error(identities):
    with pytest.raises(ConfigError, match="environment reference"):
        parse(minimal(identities=identities))


def test_credentials_by_environment_reference_are_accepted():
    config = parse(
        minimal(identities={"user": {"username": {"env": "U"}, "password": {"env": "P"}}})
    )
    assert config.identities["user"]["password"] == {"env": "P"}


def test_key_material_in_a_config_is_refused():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIE..."
    with pytest.raises(ConfigError, match="give a path"):
        parse(minimal(security={"clientCertificate": {"cert": "c.pem", "key": pem}}))


@pytest.mark.parametrize(
    "triage",
    [
        {"scenario": "values.scalar", "outcome": "pass", "summary": "x"},
        {"scenario": "values.scalar", "outcome": "project-bug"},
        {"scenario": "values.scalar", "outcome": "project-bug", "summary": "x", "runtimes": ["go"]},
    ],
    ids=["a human cannot declare a pass", "no summary", "unknown runtime"],
)
def test_a_malformed_triage_entry_is_refused(triage):
    with pytest.raises(ConfigError):
        parse(minimal(triage=[triage]))


# --- classification ---------------------------------------------------------------


def test_failures_are_published_only_as_a_human_classified_them():
    config = parse(
        minimal(
            triage=[
                {
                    "scenario": "limits.read",
                    "runtimes": ["node"],
                    "outcome": "project-bug",
                    "summary": "batch not split",
                    "tracking": "#1",
                }
            ]
        )
    )
    fail = Outcome("fail", "0 of 50")
    assert results.classify(config, "limits.read", "node", fail)["outcome"] == "project-bug"
    # The same failure on the other runtime was not triaged, so it says so.
    assert results.classify(config, "limits.read", "python", fail)["outcome"] == (
        "untriaged-failure"
    )
    # A triaged failure that now passes is reported, not silently kept.
    passed = results.classify(config, "limits.read", "node", Outcome("pass", "ok"))
    assert passed["outcome"] == "pass" and passed["stale_triage"] == "batch not split"
    assert results.classify(config, "history.raw", "node", Outcome("unsupported", "no"))[
        "outcome"
    ] == ("unsupported-optional")


# --- nothing unpublishable reaches a result --------------------------------------------


@pytest.mark.parametrize(
    "leak",
    ["opc.tcp://plc.internal:4840", "ns=3;s=Line1/Setpoint", "nsu=urn:plant;i=5"],
)
def test_a_result_with_an_endpoint_or_node_id_is_refused(leak):
    with pytest.raises(ValueError, match="refusing"):
        results.assert_publishable({"runs": [{"scenarios": [{"detail": f"failed at {leak}"}]}]})


def test_a_result_carrying_a_credential_is_refused():
    with pytest.raises(ValueError, match="credential"):
        results.assert_publishable({"detail": "login as s3cret-pw failed"}, ["s3cret-pw"])


def test_a_short_credential_is_not_mistaken_for_the_documents_own_words():
    # Milo's demo account is literally "user"; the result says "user_tokens".
    results.assert_publishable({"identities": ["user"], "user_tokens": []}, ["user"])
    results.assert_publishable({"detail": "a longer-password-x"}, ["longer-password"])


def test_tool_output_is_redacted_before_it_is_recorded(monkeypatch):
    monkeypatch.setenv("CONFORMANCE_TEST_PW", "s3cret-pw")
    config = parse(
        minimal(
            identities={
                "user": {"username": {"env": "CONFORMANCE_TEST_PW"}, "password": {"env": "X"}}
            }
        )
    )
    ctx = Context(config, "python", pki=None, discovery=Discovery([], None), lab=None)
    text = ctx.redact(
        "Failed to read ns=2;s=Line/Temp at opc.tcp://127.0.0.1:4840 as s3cret-pw; "
        "nsu=urn:x;i=1 BadUserAccessDenied"
    )
    assert "ns=2" not in text and "nsu=" not in text and "opc.tcp" not in text
    assert "s3cret-pw" not in text
    assert "BadUserAccessDenied" in text


# --- the scenario registry and the matrix -------------------------------------------------


def test_scenario_ids_are_unique_and_grouped():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert {s.group for s in SCENARIOS} == set(GROUPS)


def _document(outcomes: dict[str, str], runtimes=("python", "node")) -> dict:
    return {
        "schemaVersion": 1,
        "server": {"id": "x", "name": "X", "implementation": "X", "build_info": {}},
        "finished": "2026-09-24T00:00:00Z",
        "runs": [
            {
                "runtime": runtime,
                "scenarios": [
                    {"id": scenario, "outcome": outcome, "detail": ""}
                    for scenario, outcome in outcomes.items()
                ],
            }
            for runtime in runtimes
        ],
    }


@pytest.mark.parametrize(
    ("outcomes", "runtimes", "level"),
    [
        ({"values.scalar": "pass", "history.raw": "unsupported-optional"}, None, "Supported"),
        ({"values.scalar": "pass", "limits.read": "project-bug"}, None, "Partially supported"),
        (
            {"values.scalar": "pass", "limits.read": "server-limitation"},
            None,
            "Partially supported",
        ),
        ({"values.scalar": "pass"}, ("python",), "Unverified"),
        ({"values.scalar": "untriaged-failure"}, None, "Unverified"),
    ],
    ids=["all pass", "project bug", "limitation", "one runtime only", "untriaged"],
)
def test_support_level(outcomes, runtimes, level):
    assert matrix.support_level(_document(outcomes, runtimes or ("python", "node"))) == level


def test_the_published_matrix_is_generated_from_the_committed_results():
    """docs/compatibility.md says what compatibility/results/ says, and nothing else.

    Fails when a result file was added or changed without re-rendering, when
    the matrix was edited by hand, or when a committed result carries an
    untriaged failure or anything unpublishable.
    """
    problems = cli.check()
    assert not problems, "\n".join(problems)


def test_published_results_record_both_runtimes_and_the_servers_build_info():
    documents = results.load_all()
    assert documents, "no conformance results are committed"
    for document in documents:
        # Evidence for a commit anyone can check out, not for a working tree.
        assert not document["harness"]["git_commit"].endswith("-dirty")
        assert {run["runtime"] for run in document["runs"]} == {"python", "node"}
        assert document["server"]["build_info"].get("product_name")
        for run in document["runs"]:
            assert run["package_version"] != "unknown"
            assert {s["id"] for s in run["scenarios"]} == {s.id for s in SCENARIOS}


def test_at_least_two_independent_implementations_have_results():
    """The bar #147 set before the compatibility claim could move off 'unverified'."""
    independent = {
        d["server"]["implementation"] for d in results.load_all() if d["server"].get("independent")
    }
    assert len(independent) >= 2, independent


def test_every_result_file_is_named_for_its_server_and_date():
    for path in results.RESULTS_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert path.name == f"{document['server']['id']}-{document['finished'][:10]}.json"
