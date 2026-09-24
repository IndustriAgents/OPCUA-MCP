"""What ``completeness`` says for each way a result can come back short (#137).

``packages/server-node/test/completeness.test.mjs`` reads the same
``tests/fixtures/completeness.json`` and asserts the same objects, field for
field. The contract half is here too: the shape is declared once, every tool
that can be partial says so, and nothing that says so can be missing it.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.completeness import (
    buffer_completeness,
    drain_completeness,
    history_completeness,
    traversal_completeness,
)
from opcua_mcp_server.contract import CONTRACT

TABLE = json.loads((ROOT / "tests" / "fixtures" / "completeness.json").read_text(encoding="utf-8"))
SCHEMA = CONTRACT["completeness"]["schema"]
MAX = {
    "history": CONTRACT["limits"]["maxHistoryValues"],
    "traversal": CONTRACT["traversal"]["maxNodes"],
}


def _resolve(value, cap):
    """A table entry, with "max" standing for the cap of the table it is in."""
    if value == "max":
        return cap
    if isinstance(value, dict):
        return {key: _resolve(item, cap) for key, item in value.items()}
    return value


def _cases(table):
    return pytest.mark.parametrize(
        "case", TABLE[table], ids=[case["name"] for case in TABLE[table]]
    )


def assert_matches_schema(value: dict) -> None:
    """Every field present, nothing extra, and each of the declared type."""
    properties = SCHEMA["properties"]
    assert set(value) == set(SCHEMA["required"]) == set(properties)
    kinds = {
        "boolean": bool,
        "integer": int,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    for field, spec in properties.items():
        declared = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        assert any(
            isinstance(value[field], kinds[name])
            and not (name == "integer" and isinstance(value[field], bool))
            for name in declared
        ), f"{field}={value[field]!r} is not {declared}"
    allowed = set(properties["reasons"]["items"]["enum"])
    assert set(value["reasons"]) <= allowed


@_cases("history")
def test_a_history_read_is_described_as_the_shared_table_says(case):
    given = _resolve(case["input"], MAX["history"])
    result = history_completeness(
        returned=given["returned"],
        fetched=given["fetched"],
        wanted=given["wanted"],
        continuation_point=given["continuation_point"],
        next_start=given["next_start"],
    )
    assert result == _resolve(case["expected"], MAX["history"])
    assert_matches_schema(result)


@_cases("drain")
def test_an_event_drain_is_described_as_the_shared_table_says(case):
    result = drain_completeness(**case["input"])
    assert result == case["expected"]
    assert_matches_schema(result)


@_cases("traversal")
def test_a_browse_is_described_as_the_shared_table_says(case):
    given = _resolve(case["input"], MAX["traversal"])
    result = traversal_completeness(**given)
    assert result == _resolve(case["expected"], MAX["traversal"])
    assert_matches_schema(result)


@_cases("buffers")
def test_subscription_buffers_are_described_as_the_shared_table_says(case):
    result = buffer_completeness([{"dropped": count} for count in case["input"]["dropped"]])
    assert result == case["expected"]
    assert_matches_schema(result)


def test_complete_is_exactly_no_reasons():
    """The one field a client tests must agree with the list that explains it."""
    for table in ("history", "drain", "traversal", "buffers"):
        for case in TABLE[table]:
            expected = case["expected"]
            assert expected["complete"] is (not expected["reasons"]), case["name"]


# --- the contract half -----------------------------------------------------------

#: Every tool whose result can hold fewer records than its request covered. A
#: tool added to this list must also set `reportsCompleteness`, and the reverse.
PARTIAL_TOOLS = {
    "browse_opcua_nodes",
    "read_opcua_history",
    "read_event_history",
    "read_events",
    "list_subscriptions",
    "subscribe_opcua_nodes",
    "unsubscribe_opcua_nodes",
}


def test_every_partial_tool_says_so_and_no_other_does():
    flagged = {tool["name"] for tool in CONTRACT["tools"] if tool.get("reportsCompleteness")}
    assert flagged == PARTIAL_TOOLS


def test_the_shape_is_strict_and_documented():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    for field, spec in SCHEMA["properties"].items():
        assert spec.get("description", "").strip(), f"completeness.{field} is undocumented"


def test_every_reason_is_explained():
    assert set(SCHEMA["properties"]["reasons"]["items"]["enum"]) == {
        key for key in CONTRACT["completeness"]["reasons"] if not key.startswith("$")
    }


def test_both_runtimes_advertise_it_from_the_contract():
    """The outputSchema is built in two places; both must read the contract."""
    python = (
        ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server" / "server.py"
    ).read_text(encoding="utf-8")
    node = (ROOT / "packages" / "server-node" / "src" / "tools.ts").read_text(encoding="utf-8")
    assert 'CONTRACT["completeness"]' in python and "reportsCompleteness" in python
    assert "CONTRACT.completeness.schema" in node and "reportsCompleteness" in node
