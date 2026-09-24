"""The write codec: what a JSON value becomes on the wire, per OPC UA type.

The rules are the shared table in ``tests/fixtures/write-coercion.json``, which
``packages/server-node/test/variant-codec.test.mjs`` drives through the Node
server's codec too — refusal messages included, because a write that one runtime
sends and the other refuses is a different plant depending on which package was
installed (#157).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.numeric import js_number, json_text
from opcua_mcp_server.policy import as_number
from opcua_mcp_server.variant_codec import convert_for_variant

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "write-coercion.json").read_text(encoding="utf-8")
)
CASES = FIXTURE["cases"]


def _case_id(case: dict) -> str:
    kind = "[]" if case.get("array") else ""
    return f"{case['type']}{kind} <- {json.dumps(case['value'], ensure_ascii=False)}"


def _normalized(type_name: str, value):
    """The converted value in the fixture's runtime-neutral form."""
    if type_name in ("Int64", "UInt64"):
        return str(value)
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond // 1000:03d}Z"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, ua.LocalizedText):
        return value.Text
    if isinstance(value, ua.QualifiedName):
        return value.Name
    return value


@pytest.mark.parametrize("case", CASES, ids=[_case_id(case) for case in CASES])
def test_the_shared_table(case):
    variant_type = ua.VariantType[case["type"]]
    is_array = case.get("array", False)
    if "error" in case:
        with pytest.raises(ValueError) as excinfo:
            convert_for_variant(case["value"], variant_type, is_array)
        assert str(excinfo.value) == case["error"]
        return

    converted = convert_for_variant(case["value"], variant_type, is_array)
    if is_array:
        converted = [_normalized(case["type"], item) for item in converted]
    else:
        converted = _normalized(case["type"], converted)
    assert converted == case["expected"]
    # `True == 1` in Python, so a Boolean has to be checked for being one.
    if isinstance(case["expected"], bool):
        assert isinstance(converted, bool)


NUMBERS = FIXTURE["numbers"]


@pytest.mark.parametrize(
    "case", NUMBERS, ids=[json.dumps(case["value"], ensure_ascii=False) for case in NUMBERS]
)
def test_the_policy_reads_numbers_with_the_same_grammar(case):
    """A bound compares what the codec would write, by the same grammar."""
    assert as_number(case["value"]) == case["expected"]


def test_uint64_does_not_lose_precision():
    assert convert_for_variant(str(2**64 - 1), ua.VariantType.UInt64) == 2**64 - 1


def test_an_integer_python_holds_exactly_is_refused_as_node_would_see_it():
    """Python's JSON parser keeps 2**53+1 whole; JavaScript's cannot.

    The table cannot say this — a JSON file read by Node has already lost the
    digit — so it is asserted here: Python refuses it the same way, rather than
    writing a value Node would have written differently.
    """
    with pytest.raises(ValueError, match="lost precision"):
        convert_for_variant(2**53 + 1, ua.VariantType.Int64)
    with pytest.raises(ValueError, match="outside the Double range"):
        convert_for_variant(10**400, ua.VariantType.Double)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, "1.5"),
        (5.0, "5"),
        (-0.0, "0"),
        (1e-7, "1e-7"),
        (1e-6, "0.000001"),
        (0.001, "0.001"),
        (1e16, "10000000000000000"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (2.0**70, "1.1805916207174113e+21"),
        (2.0**69, "590295810358705700000"),
        (123456.789, "123456.789"),
        (-3.5e38, "-3.5e+38"),
        (5e-324, "5e-324"),
    ],
)
def test_numbers_are_written_as_javascript_writes_them(value, expected):
    """Messages that echo a number must be identical on both runtimes."""
    assert js_number(value) == expected


def test_json_text_writes_what_json_stringify_writes():
    assert json_text({"v": [1, 2.0, "é", None, True]}) == '{"v":[1,2,"é",null,true]}'
    assert json_text(2**53 + 1) == "9007199254740992"
