"""Unit tests for node-id canonicalisation and namespace-URI resolution.

The cases live in ``tests/fixtures/node-id-forms.json`` rather than here,
because the Node server reads the same file (the "node id forms" suite in
packages/server-node/test/unit.test.mjs). Both runtimes parse the *same* policy
file, so a form one canonicalises and the other does not is an allowlist that
authorises different writes depending on which server the operator started.

Neither suite may skip a case, so adding one to the fixture forces both runtimes
to handle it.

No OPC UA server and no MCP transport.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opcua_mcp_server.node_ids import canonical_node_id, namespace_uri_form, resolve_node_id

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "node-id-forms.json").read_text(
        encoding="utf-8"
    )
)
NAMESPACES = FIXTURE["namespaces"]


@pytest.mark.parametrize("case", FIXTURE["canonical"], ids=lambda case: case["name"])
def test_canonical_spelling(case):
    assert canonical_node_id(case["given"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["resolved"], ids=lambda case: case["name"])
def test_resolution_against_a_namespace_array(case):
    assert resolve_node_id(case["given"], NAMESPACES) == case["expected"]


def test_every_fixture_case_is_exercised():
    """The fixture is the contract; a suite that quietly skipped half of it would pass."""
    assert len(FIXTURE["canonical"]) >= 10
    assert len(FIXTURE["resolved"]) >= 7


def test_the_namespace_uri_form_splits_on_the_first_separator_only():
    """An `s=` identifier may contain ';', and truncating one would repoint it.

    Not in the shared fixture because it asserts on the *parts*, not on a
    canonical string — but it is the reason the split is written the way it is.
    """
    assert namespace_uri_form("nsu=urn:plant:line-a;s=Tag;with;semicolons") == (
        "urn:plant:line-a",
        "s=Tag;with;semicolons",
    )


def test_a_plain_node_id_is_not_the_namespace_uri_form():
    assert namespace_uri_form("ns=2;i=5") is None
    assert namespace_uri_form("i=2253") is None


def test_resolution_without_a_namespace_array_cannot_resolve_a_uri():
    """Unknown is not empty: before a session there is nothing to resolve against.

    The policy treats this as a denial. Resolving optimistically would authorise
    a write to whatever node happened to sit at the guessed index.
    """
    assert resolve_node_id("nsu=urn:plant:line-a;i=5", []) is None
    # ...while an index form needs no server to be understood.
    assert resolve_node_id("ns=2;i=5", []) == "ns=2;i=5"
