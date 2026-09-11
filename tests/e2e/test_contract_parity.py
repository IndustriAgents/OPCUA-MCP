"""Contract-parity tests: both MCP servers must agree with contract/tools.json.

The Node server builds its tools/list directly from the contract; the Python
server sources its tool descriptions and capability node IDs from it. This test
asserts that, against the mock server, BOTH servers advertise exactly the
contract's applicable tools, with matching descriptions and parameter sets, and
that what they actually return matches the contract's declared ``resultShape`` —
so the two implementations cannot silently drift.

Run as part of the normal suite:
    uv sync --all-packages && cd tests && uv run --no-sync pytest -v
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, records_of, text_of

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text())

# The bundled mock server enables history but advertises no aggregate functions,
# so the contract tools applicable here are those with no capability + history.
_MOCK_CAPS = {None, "history"}
EXPECTED = {t["name"]: t for t in CONTRACT["tools"] if t["capability"] in _MOCK_CAPS}

RESULT_SHAPES = CONTRACT["resultShapes"]

# Minimal JSON-Schema evaluation: the contract's record schemas use only these
# keywords, and a `jsonschema` dependency for a handful of asserts would be more
# machinery than the check is worth.
_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _matches_type(value, declared) -> bool:
    """True when ``value`` satisfies a schema ``type`` (a name, a list, or absent)."""
    if declared is None:
        return True  # No declared type — any JSON value is allowed.
    names = [declared] if isinstance(declared, str) else declared
    # `bool` is a subclass of `int`, so a boolean must not pass as a number.
    if isinstance(value, bool) and "boolean" not in names:
        return False
    return any(isinstance(value, _JSON_TYPES[name]) for name in names)


def assert_matches_result_shape(records: list[dict], shape_name: str, context: str) -> None:
    """Assert every record satisfies the named shape from ``contract/tools.json``.

    This is what makes the shape enforceable rather than merely documented: the
    contract file is the assertion, so changing either server's output without
    changing the contract fails here.
    """
    record_schema = RESULT_SHAPES[shape_name]["items"]
    properties = record_schema["properties"]
    required = set(record_schema["required"])

    for record in records:
        assert isinstance(record, dict), f"{context}: record is not an object: {record!r}"
        assert set(record) >= required, (
            f"{context}: record is missing {sorted(required - set(record))}: {record!r}"
        )
        if record_schema.get("additionalProperties") is False:
            assert set(record) <= set(properties), (
                f"{context}: record has fields outside the contract "
                f"{sorted(set(record) - set(properties))}: {record!r}"
            )
        for field, spec in properties.items():
            assert _matches_type(record[field], spec.get("type")), (
                f"{context}: {field}={record[field]!r} does not match "
                f"declared type {spec.get('type')!r}"
            )


def _props_required(schema: dict) -> tuple[set, set]:
    schema = schema or {}
    return set(schema.get("properties", {})), set(schema.get("required", []))


@pytest.fixture(params=["python", "node"])
def impl_params(request, opcua_server):
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, opcua_server)


async def test_servers_match_contract(impl_params):
    impl, params = impl_params
    async with connect(params) as session:
        listed = await session.list_tools()
    advertised = {t.name: t for t in listed.tools}

    # 1) Exact tool-name parity with the contract (gated to the mock's caps).
    assert set(advertised) == set(EXPECTED), (
        f"{impl}: advertised tools diverge from contract; "
        f"missing={set(EXPECTED) - set(advertised)} extra={set(advertised) - set(EXPECTED)}"
    )

    # 2) Description + parameter parity per tool. Descriptions must match exactly
    #    (both servers source them from the contract). Schemas are compared by
    #    property names + required set to tolerate FastMCP vs node-opcua schema
    #    representation differences while still catching real parameter drift.
    for name, spec in EXPECTED.items():
        tool = advertised[name]
        assert tool.description == spec["description"], (
            f"{impl}/{name}: description differs from contract"
        )
        # `inputSchema` on the wire, `input_schema` on the SDK's model. Read the
        # attribute directly rather than via `getattr(..., {})`, so another rename
        # in the SDK fails here instead of quietly comparing against an empty set.
        want_props, want_req = _props_required(spec["inputSchema"])
        got_props, got_req = _props_required(tool.input_schema or {})
        assert got_props == want_props, (
            f"{impl}/{name}: params {got_props} != contract {want_props}"
        )
        assert got_req == want_req, f"{impl}/{name}: required {got_req} != contract {want_req}"


async def test_history_result_matches_the_contract_shape(impl_params):
    """Both servers' history output must match the contract's declared shape.

    Tool *names* were unified from the start, but the response shape was not: the
    Node server returned raw node-opcua ``DataValue`` JSON while the Python
    server returned flat records, so a client that learned one misread the other
    (issue #23). The contract now declares the shape and this asserts it.
    """
    impl, params = impl_params
    spec = EXPECTED["read_history_opcua_node"]
    assert spec["resultShape"] == "historyRecords"

    async with connect(params) as session:
        result = await session.call_tool(
            "read_history_opcua_node", {"node_id": NODE["Temperature"], "num_values": 3}
        )

    assert not result.is_error, text_of(result)
    records = records_of(result)
    assert records, f"{impl}: no history records to check the shape against"
    assert_matches_result_shape(records, spec["resultShape"], f"{impl}/read_history_opcua_node")
