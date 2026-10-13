"""The operator policy's alarm scope (``alarm-scope.json``).

``packages/server-node/test/alarm-scope.test.mjs`` drives the same table. An
alarm whose source or severity cannot be read while the policy restricts it is
refused: nothing is acknowledged on the policy's behalf that the policy cannot
be checked against.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.policy_check import check_alarm_scope

CASES = json.loads((ROOT / "tests" / "fixtures" / "alarm-scope.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_alarm_scope_answers_the_shared_table(case):
    assert check_alarm_scope(case["input"]) == case["expect"]
