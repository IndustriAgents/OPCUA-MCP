"""What a request may carry before it reaches the OPC UA server (issue #139).

``packages/server-node/test/request-limits.test.mjs`` reads the same
``tests/fixtures/request-limits.json`` and asserts the same verdict and the same
sentence. Each case runs through everything that happens before the network, in
the order ``call_tool`` runs it: the request-wide bounds, then the contract
schema whose ``maxItems`` carries the per-tool counts.
"""

from __future__ import annotations

import base64
import json
import math

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.limits import (
    MAX_BYTE_STRING_BYTES,
    MAX_STRING_BYTES,
    check_request_bounds,
    event_buffer_size,
)
from opcua_mcp_server.validation import validate_arguments
from opcua_mcp_server.variant_codec import convert_for_variant

TABLE = json.loads(
    (ROOT / "tests" / "fixtures" / "request-limits.json").read_text(encoding="utf-8")
)
SPECS = {tool["name"]: tool for tool in CONTRACT["tools"]}


def expand(value):
    """Build what a directive describes; ``request-limits.test.mjs`` mirrors this."""
    if isinstance(value, dict):
        if set(value) == {"$string"}:
            spec = value["$string"]
            count, character = (spec, "a") if isinstance(spec, int) else spec
            return character * count
        if set(value) == {"$array"}:
            item, count = value["$array"]
            return [expand(item) for _ in range(count)]
        if set(value) == {"$nest"}:
            depth, leaf = value["$nest"]
            built = expand(leaf)
            for _ in range(depth):
                built = [built]
            return built
        return {key: expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item) for item in value]
    return value


def precheck(tool: str, arguments: dict) -> None:
    """What ``PolicyMCPServer.call_tool`` runs before authorization, in its order."""
    check_request_bounds(tool, arguments)
    validate_arguments(tool, SPECS[tool]["inputSchema"], arguments)


def test_the_table_was_written_against_the_contracts_limits():
    """A limit changed in the contract must be changed in the table on purpose."""
    for name, value in TABLE["limits"].items():
        assert CONTRACT["limits"][name] == value, (
            f"limits.{name} is {CONTRACT['limits'][name]} in the contract but {value} in "
            f"request-limits.json; update the table's numbers and messages with it"
        )


@pytest.mark.parametrize(
    "case", TABLE["requests"], ids=[case["name"] for case in TABLE["requests"]]
)
def test_a_request_is_bounded_as_the_shared_table_says(case):
    arguments = expand(case["arguments"])
    if case["error"] is None:
        precheck(case["tool"], arguments)
        return
    with pytest.raises(ValueError) as raised:
        precheck(case["tool"], arguments)
    assert str(raised.value) == case["error"]


@pytest.mark.parametrize(
    "case", TABLE["byteStrings"], ids=[case["name"] for case in TABLE["byteStrings"]]
)
def test_a_bytestring_is_bounded_once_decoded(case):
    text = base64.b64encode(bytes(case["bytes"])).decode("ascii")
    if case["error"] is None:
        assert len(convert_for_variant(text, ua.VariantType.ByteString)) == case["bytes"]
        return
    with pytest.raises(ValueError) as raised:
        convert_for_variant(text, ua.VariantType.ByteString)
    assert str(raised.value) == case["error"]


@pytest.mark.parametrize(
    "case",
    TABLE["eventBufferSizes"],
    ids=[case["name"] for case in TABLE["eventBufferSizes"]],
)
def test_the_event_buffer_is_clamped_as_the_shared_table_says(case):
    assert event_buffer_size(case["requested"]) == case["applied"]


def test_every_bytestring_this_server_would_write_also_fits_as_a_string():
    """The base64 of the largest legal ByteString must pass the string limit first.

    Otherwise ``maxByteStringBytes`` would be a promise the request walk breaks
    before the codec is ever reached, and the effective ByteString limit would be
    three quarters of the string limit rather than the number the contract says.
    """
    assert 4 * math.ceil(MAX_BYTE_STRING_BYTES / 3) <= MAX_STRING_BYTES
