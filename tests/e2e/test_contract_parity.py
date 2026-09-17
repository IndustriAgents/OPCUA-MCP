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

import asyncio
import json

import pytest
from conftest import ROOT
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, records_of, text_of

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text())

# The bundled mock server enables history but advertises no aggregate functions,
# so the contract tools applicable here are those with no capability + history.
_MOCK_CAPS = {None, "history"}
EXPECTED = {t["name"]: t for t in CONTRACT["tools"] if t["capability"] in _MOCK_CAPS}

# Resources are not capability-gated: both servers advertise all of them always.
EXPECTED_RESOURCES = {r["uri"]: r for r in CONTRACT["resources"]}

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


def _declared_types(schema: dict) -> dict[str, set]:
    """Each property's advertised JSON-Schema type(s), for those that declare any.

    Compared across runtimes because the *type* is as much a part of the wire
    contract as the name: the Python server derives its schema from the function
    annotations, so an `int` where the contract says `number` both advertises a
    different schema and makes the SDK reject an input the Node server accepts.

    A set rather than one name, because `MCPServer` renders an optional
    `T | None` parameter as `anyOf: [{type: T}, {type: "null"}]` and not as a
    bare `type`. The contract declares the type of the *value*; "or null" is
    just how one runtime spells "you may omit this". Properties neither side
    types are skipped rather than guessed at.
    """
    types = {}
    for name, spec in (schema or {}).get("properties", {}).items():
        names = set()
        declared = spec.get("type")
        if declared is not None:
            names |= {declared} if isinstance(declared, str) else set(declared)
        for branch in [*spec.get("anyOf", []), *spec.get("oneOf", [])]:
            branch_type = branch.get("type")
            if branch_type is not None:
                names |= {branch_type} if isinstance(branch_type, str) else set(branch_type)
        if names:
            types[name] = names
    return types


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

        # Types too. Without this, `buffer_size: int` on the Python side passed
        # while advertising `integer` against the contract's `number` — and
        # rejected a `7.9` the Node server happily truncated.
        want_types = _declared_types(spec["inputSchema"])
        got_types = _declared_types(tool.input_schema or {})
        for prop, wanted in want_types.items():
            offered = got_types.get(prop, set())
            assert wanted <= offered, (
                f"{impl}/{name}.{prop}: advertises {sorted(offered)}, "
                f"which does not cover the contract's {sorted(wanted)}"
            )

        if shape_name := spec.get("resultShape"):
            assert tool.output_schema == {
                "type": "object",
                "properties": {"result": RESULT_SHAPES[shape_name]},
                "required": ["result"],
                "additionalProperties": False,
            }, f"{impl}/{name}: outputSchema differs from the shared result shape"


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
    assert result.structured_content == {"result": records}, (
        f"{impl}: structured history result differs from compatibility content"
    )
    assert records, f"{impl}: no history records to check the shape against"
    assert_matches_result_shape(records, spec["resultShape"], f"{impl}/read_history_opcua_node")


async def test_servers_advertise_the_contract_resources(impl_params):
    """Both servers must offer the same resources, worded the same.

    A resource is as much a client-visible surface as a tool: an agent told to
    re-read `opcua://subscriptions` has to find it under that URI, with that
    description, whichever runtime it is talking to.
    """
    impl, params = impl_params
    async with connect(params) as session:
        listed = await session.list_resources()
    advertised = {str(r.uri): r for r in listed.resources}

    assert set(advertised) == set(EXPECTED_RESOURCES), (
        f"{impl}: advertised resources diverge from contract; "
        f"missing={set(EXPECTED_RESOURCES) - set(advertised)} "
        f"extra={set(advertised) - set(EXPECTED_RESOURCES)}"
    )

    for uri, spec in EXPECTED_RESOURCES.items():
        resource = advertised[uri]
        assert resource.name == spec["name"], f"{impl}/{uri}: name differs from contract"
        assert resource.description == spec["description"], (
            f"{impl}/{uri}: description differs from contract"
        )
        assert resource.mime_type == spec["mimeType"], (
            f"{impl}/{uri}: mimeType differs from contract"
        )


async def test_subscription_resource_matches_the_contract_shape(impl_params):
    """Reading the resource must yield the shape the contract declares for it.

    Checked with a live subscription, because an empty document would satisfy the
    shape without ever exercising a record.
    """
    impl, params = impl_params
    spec = EXPECTED_RESOURCES["opcua://subscriptions"]
    body = spec["body"]

    async with connect(params) as session:
        created = await session.call_tool(
            "subscribe_opcua_node",
            {"node_id": NODE["Temperature"], "publishing_interval": 200, "buffer_size": 5},
        )
        assert not created.is_error, text_of(created)
        await asyncio.sleep(2)
        result = await session.read_resource(spec["uri"])

    contents = result.contents
    assert len(contents) == 1, f"{impl}: expected one content block, got {len(contents)}"
    assert contents[0].mime_type == spec["mimeType"], f"{impl}: resource mimeType differs"

    document = json.loads(contents[0].text)
    assert set(document) == {body["recordsKey"]}, (
        f"{impl}: resource document keys {sorted(document)} != [{body['recordsKey']!r}]"
    )
    records = document[body["recordsKey"]]
    assert records, f"{impl}: the resource reported no subscriptions"
    assert_matches_result_shape(records, body["resultShape"], f"{impl}/{spec['uri']}")
    # The nested changes are history records, and the contract says so.
    for record in records:
        assert_matches_result_shape(
            record["changes"], "historyRecords", f"{impl}/{spec['uri']}/changes"
        )


async def test_subscription_tools_match_the_contract_shape(impl_params):
    """`subscribe_opcua_node` and `list_subscriptions` share one record shape."""
    impl, params = impl_params
    assert EXPECTED["subscribe_opcua_node"]["resultShape"] == "subscriptionRecords"
    assert EXPECTED["list_subscriptions"]["resultShape"] == "subscriptionRecords"

    async with connect(params) as session:
        created = await session.call_tool(
            "subscribe_opcua_node", {"node_id": NODE["Temperature"], "publishing_interval": 200}
        )
        assert not created.is_error, text_of(created)
        listed = await session.call_tool("list_subscriptions", {})
        assert not listed.is_error, text_of(listed)

    for name, result in (("subscribe_opcua_node", created), ("list_subscriptions", listed)):
        records = records_of(result)
        assert records, f"{impl}/{name}: no subscription records to check the shape against"
        assert_matches_result_shape(records, "subscriptionRecords", f"{impl}/{name}")
