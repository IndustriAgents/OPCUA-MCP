"""The contract-schema validator, driven by the table both runtimes share.

``packages/server-node/test/validation.test.mjs`` reads the same
``tests/fixtures/argument-validation.json`` and asserts the same verdict and the
same sentence. A rule that holds here and not there is the divergence this file
exists to stop — the two servers must refuse the same calls, and say the same
thing when they do.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.validation import SUPPORTED_KEYWORDS, validate_arguments

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "argument-validation.json").read_text(encoding="utf-8")
)["cases"]
SPECS = {tool["name"]: tool for tool in CONTRACT["tools"]}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_validator_answers_the_shared_table(case):
    spec = SPECS[case["tool"]]
    if case["error"] is None:
        validate_arguments(case["tool"], spec["inputSchema"], case["arguments"])
        return
    with pytest.raises(ValueError) as raised:
        validate_arguments(case["tool"], spec["inputSchema"], case["arguments"])
    assert str(raised.value) == case["error"]


def test_arguments_that_are_not_an_object_are_refused():
    with pytest.raises(ValueError) as raised:
        validate_arguments("read_opcua_nodes", SPECS["read_opcua_nodes"]["inputSchema"], ["a"])
    assert str(raised.value) == "read_opcua_nodes argument arguments must be an object"


def _keywords(schema) -> set[str]:
    """Every JSON-Schema keyword appearing anywhere in ``schema``."""
    if not isinstance(schema, dict):
        return set()
    found = {key for key in schema if not key.startswith("$")}
    for key in ("items",):
        found |= _keywords(schema.get(key))
    for subschema in (schema.get("properties") or {}).values():
        found |= _keywords(subschema)
    return found


def test_the_contract_uses_no_keyword_the_validator_would_ignore():
    """A keyword neither validator implements is a constraint silently not applied.

    The failure this prevents is quiet: someone adds ``enum`` or ``pattern`` to
    the contract, both runtimes ignore it, and the schema advertises a promise
    nothing keeps.
    """
    for tool in CONTRACT["tools"]:
        used = _keywords(tool["inputSchema"])
        unsupported = used - SUPPORTED_KEYWORDS
        assert not unsupported, (
            f"{tool['name']} declares {sorted(unsupported)}, which neither validator "
            f"implements; add it to validation.py and validation.ts or drop it"
        )
