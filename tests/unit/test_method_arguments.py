"""What type a method argument is sent as (#157).

The shared table is ``tests/fixtures/method-arguments.json``;
``packages/server-node/test/method-arguments.test.mjs`` drives the same one
through the Node server. The supertype walk is given the table's hierarchy in
place of a live server's inverse HasSubtype browse.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.method_arguments import built_in_type, guess_variant

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "method-arguments.json").read_text(encoding="utf-8")
)
SUPERTYPES = FIXTURE["supertypes"]


@pytest.mark.parametrize(
    "case", FIXTURE["declared"], ids=[case["name"] for case in FIXTURE["declared"]]
)
def test_a_declared_type_resolves_to_its_built_in_base(case):
    if "error" in case:
        with pytest.raises(ValueError) as excinfo:
            built_in_type(case["data_type"], SUPERTYPES.get)
        assert str(excinfo.value) == case["error"]
    else:
        assert built_in_type(case["data_type"], SUPERTYPES.get).name == case["expected"]


@pytest.mark.parametrize(
    "case", FIXTURE["guessed"], ids=[case["name"] for case in FIXTURE["guessed"]]
)
def test_an_undeclared_argument_is_guessed_the_same_way(case):
    if "error" in case:
        with pytest.raises(ValueError) as excinfo:
            guess_variant(case["value"], 0)
        assert str(excinfo.value) == case["error"]
    else:
        variant = guess_variant(case["value"], 0)
        assert variant.VariantType == ua.VariantType[case["expected"]["type"]]
        assert variant.Value == case["expected"]["value"]
        # `True == 1` in Python; the value must be of the Variant's own kind.
        expected_kind = {"Boolean": bool, "Double": float, "String": str}
        assert type(variant.Value) is expected_kind[case["expected"]["type"]]
