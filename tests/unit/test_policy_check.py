"""What the policy check on connect finds (``policy-check.json``).

``packages/server-node/test/policy-check.test.mjs`` drives the same table. The
input is what resolving the policy against a server produced; the findings must
match exactly and in order, because they are printed at startup and reported by
``get_server_status`` on both runtimes.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.policy_check import policy_findings

CASES = json.loads((ROOT / "tests" / "fixtures" / "policy-check.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_policy_check_answers_the_shared_table(case):
    assert policy_findings(case["input"]) == case["expect"]
