"""Native formatting and the MCP result boundary share Node's fixture output."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from conftest import ROOT
from mcp.types import CallToolResult, TextContent
from opcua_mcp_server.datetimes import format_iso_utc
from opcua_mcp_server.numeric import array_index, json_text
from opcua_mcp_server.policy import format_number
from opcua_mcp_server.result_text import normalize_result_text, pretty_json

FIXTURE = json.loads((ROOT / "tests/fixtures/result-text.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_json_text(case):
    assert pretty_json(case["value"]) == case["pretty"]
    assert json_text(case["value"]) == case["compact"]


@pytest.mark.parametrize("case", FIXTURE["timestamps"], ids=lambda case: case["name"])
def test_native_timestamps(case):
    value = datetime.fromisoformat(case["input"].replace("Z", "+00:00"))
    assert format_iso_utc(value) == case["python"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (1e-5, "0.00001"),
        (1e16, "10000000000000000"),
        (-1.0, "-1"),
        (1e-7, "1e-7"),
        (float("inf"), "infinity"),
    ],
)
def test_refusal_numbers(value, expected):
    assert format_number(value) == expected


@pytest.mark.parametrize(
    "value", [{"unit": "°C", "value": 1000.0}, [{"unit": "°C"}, {"alarm": "温度"}], []]
)
def test_boundary_preserves_structured_content_and_notices(value):
    records = value if isinstance(value, list) else [value]
    content = [TextContent(type="text", text=json.dumps(record)) for record in records]
    if not records:
        content = [TextContent(type="text", text="[]")]
    notice = TextContent(type="text", text="Truncated at the configured limit.")
    content.append(notice)
    source = CallToolResult(
        content=content, structured_content={"result": value, "completeness": {"truncated": True}}
    )
    result = normalize_result_text(source)
    assert result.structured_content is source.structured_content
    assert [item.text for item in result.content] == [pretty_json(record) for record in records] + [
        notice.text
    ]
    assert source.content == content


def test_errors_and_unstructured_messages_are_left_intact():
    for result in [
        CallToolResult(content=[TextContent(type="text", text="failure")], is_error=True),
        CallToolResult(content=[TextContent(type="text", text="notice")]),
    ]:
        assert normalize_result_text(result) is result


def test_long_numeric_property_name_is_not_parsed_as_an_integer():
    key = "9" * 5000
    assert array_index(key) is None
    assert pretty_json({key: 1}) == '{\n  "' + key + '": 1\n}'
