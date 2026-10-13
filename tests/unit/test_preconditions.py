"""Interlocks from the operator policy's ``control.preconditions`` (``preconditions.json``).

``packages/server-node/test/preconditions.test.mjs`` drives the same table. A
requirement holds only on a Good reading: an interlock whose state cannot be read
is not one that is satisfied.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.policy_check import check_preconditions

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "preconditions.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_preconditions_answer_the_shared_table(case):
    assert check_preconditions(case["input"]) == case["expect"]
