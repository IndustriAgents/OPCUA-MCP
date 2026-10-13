"""How each write is planned from its node's own attributes (``write-plan.json``).

``packages/server-node/test/write-plan.test.mjs`` drives the same table through
the Node runtime. A write used to be judged by reading the node's current value
and copying its type, which cannot tell a read-only node from a writable one, a
list from a scalar or an enumeration's states from bare numbers — and fails
outright on a node that can be written but not read. The table pins what the
attributes decide instead: send (and as what), skip one node, or refuse the
batch.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.write_plan import plan_write

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "write-plan.json").read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_write_is_planned_as_the_shared_table_says(case):
    plan = plan_write(case["request"], case["facts"], case["engineering"], case["options"])
    assert plan == case["expect"]


def test_a_resolved_state_is_the_number_not_its_spelling():
    """A numeric string naming a state is sent as the number, typed as a JSON number."""
    case = next(c for c in CASES if c["name"].endswith("as a numeric string, as a number"))
    plan = plan_write(case["request"], case["facts"], case["engineering"], case["options"])
    assert isinstance(plan["value"], int) and not isinstance(plan["value"], bool)
