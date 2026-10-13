"""Whether a method call can work at all, from what its two nodes publish (``method-plan.json``).

``packages/server-node/test/method-plan.test.mjs`` drives the same table. Each
refusal is a call the OPC UA server would turn down anyway — a Variable called as
a method, a method switched off, one that is not the object's — said before
anything is encoded or sent, in a sentence that names what to do instead.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.write_plan import plan_call

CASES = json.loads((ROOT / "tests" / "fixtures" / "method-plan.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_call_is_planned_as_the_shared_table_says(case):
    assert plan_call(case["input"]) == case["expect"]
