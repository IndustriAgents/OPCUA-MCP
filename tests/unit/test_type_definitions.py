"""Which type a browsed node is reported as (issue #120).

A browse record named a node, its class and its parent — so every alarm, every
pump and every folder came back as ``Object``. ``HasTypeDefinition`` is what
says which, and reading it is the whole of #120.

The rule for *choosing* one is small and lives in a shared table, because a
node's reported type is not an error anyone sees: it is a different string, and
the two runtimes disagreeing about it would surface only as an agent reaching a
different conclusion on one of them.
``packages/server-node/test/type-definitions.test.mjs`` drives the same table.

What is not here is the browse itself — that needs a real address space, and it
is in ``tests/e2e/test_type_definitions_e2e.py`` against both mocks.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.server import type_definition_of

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "type-definitions.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_rule_answers_the_shared_table(case):
    assert type_definition_of(case["is_good"], case["browse_names"]) == case["type_definition"]


def test_the_reference_type_is_the_standard_one():
    """``ns=0;i=40`` is HasTypeDefinition, fixed by OPC UA Part 3.

    It lives in the contract rather than as a literal in each runtime for the
    same reason every other node id does — but unlike most of them it is never
    seen in an output, so a typo here would not produce a wrong answer. It would
    produce *no* answer, on every node, with no error: a browse for a reference
    type that does not exist succeeds and returns nothing.
    """
    from opcua import ua

    assert CONTRACT["traversal"]["hasTypeDefinitionNodeId"] == "ns=0;i=40"
    # python-opcua files every standard node under ObjectIds; node-opcua puts
    # this one under ReferenceTypeIds, which is where it belongs. Same 40 either
    # way, fixed by the spec, which is what lets the Node half assert the same
    # number.
    assert ua.ObjectIds.HasTypeDefinition == 40


def test_the_batch_is_chunked_below_a_default_walk():
    """One browse per chunk, and a chunk smaller than what one walk can return.

    `MaxNodesPerBrowse` is an operational limit a conformant server publishes and
    enforces. The default walk returns up to 500 nodes, so an unchunked request
    would be one a server is entitled to refuse — and refusing it would lose
    every type in the result, silently, since this is best-effort.
    """
    traversal = CONTRACT["traversal"]
    assert traversal["maxTypeDefinitionsPerRequest"] < traversal["defaultMaxNodes"]
