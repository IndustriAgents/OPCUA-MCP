"""What a node's DataType is written as (``data-type-resolution.json``).

``packages/server-node/test/data-type-resolution.test.mjs`` walks the same
supertype chains. A type that cannot be resolved is a check that is skipped, so
the function never raises — not on a loop, not on a dead end, not on a lookup
that fails.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.node_facts import resolve_data_type

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "data-type-resolution.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_data_type_resolves_as_the_shared_table_says(case):
    supertypes = case["supertypes"]
    assert resolve_data_type(case["data_type_id"], supertypes.get) == case["expect"]


def test_a_lookup_that_fails_is_unresolved_rather_than_an_error():
    def broken(_node_id: str) -> str | None:
        raise ConnectionError("the session died")

    assert resolve_data_type("ns=2;i=3001", broken) == {"data_type": None, "enumeration": False}


def test_the_short_spelling_of_a_built_in_resolves_like_the_long_one():
    assert resolve_data_type("i=11", {}.get) == {"data_type": "Double", "enumeration": False}
