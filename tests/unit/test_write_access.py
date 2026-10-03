"""What ``read_opcua_nodes`` says about writing each node (``write-access.json``).

``packages/server-node/test/write-access.test.mjs`` drives the same table. Every
key is compared, so the two runtimes report the same shape, the same reason and
the same tightest bounds for the same node and policy.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.write_plan import write_access

CASES = json.loads((ROOT / "tests" / "fixtures" / "write-access.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_write_access_answers_the_shared_table(case):
    actual = write_access(case["input"])
    assert list(actual) == list(case["expect"]), "every key, in the contract's order"
    assert actual == case["expect"]
