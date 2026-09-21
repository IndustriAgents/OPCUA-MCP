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
import copy
import json

import pytest
from conftest import ROOT
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, records_of, text_of

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))

# The bundled mock server enables history but advertises no aggregate functions,
# so a tool is applicable here if it needs nothing or accepts "history".
_MOCK_CAPS = {"history"}
EXPECTED = {
    t["name"]: t
    for t in CONTRACT["tools"]
    if not t["capabilities"] or set(t["capabilities"]) & _MOCK_CAPS
}

#: Arguments both servers withhold when the connected server cannot honour them.
#:
#: Capability gating moved down a level when the history and aggregate tools
#: merged: `read_opcua_history` is offered whenever the server reports
#: HistoricalAccess, and `aggregate_function` appears on it only if the server
#: *also* advertises aggregates. Against this mock it does not, so both runtimes
#: must withhold these two — which is the property tool-level gating used to
#: have, applied to an argument. `test_aggregate_e2e.py` drives the other side
#: against the aggregate mock, where they must be present.
AGGREGATE_ONLY_PARAMS = {"aggregate_function", "processing_interval"}


def _expected_schema(spec: dict) -> dict:
    """The contract's input schema for `spec`, minus what this mock cannot support.

    A deep copy, because the aggregate-only arguments are removed from it: this
    mock advertises no aggregate functions, so neither runtime may offer them.
    """
    schema = copy.deepcopy(spec["inputSchema"])
    for argument in AGGREGATE_ONLY_PARAMS:
        schema.get("properties", {}).pop(argument, None)
        if argument in schema.get("required", []):
            schema["required"].remove(argument)
    return schema


# Resources are not capability-gated: both servers advertise all of them always.
EXPECTED_RESOURCES = {r["uri"]: r for r in CONTRACT["resources"]}

RESULT_SHAPES = CONTRACT["resultShapes"]

# Minimal JSON-Schema evaluation: the contract's record schemas use only these
# keywords, and a `jsonschema` dependency for a handful of asserts would be more
# machinery than the check is worth.
_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    # JSON Schema's "integer" is a whole number, not Python's `int`. The `bool`
    # exclusion below covers the other half of the trap.
    "integer": int,
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
    # A float that is not whole does not satisfy "integer", whatever Python says
    # about isinstance.
    if isinstance(value, float) and names == ["integer"]:
        return value.is_integer()
    return any(isinstance(value, _JSON_TYPES[name]) for name in names)


def assert_matches_result_shape(records: list[dict], shape_name: str, context: str) -> None:
    """Assert every record satisfies the named shape from ``contract/tools.json``.

    This is what makes the shape enforceable rather than merely documented: the
    contract file is the assertion, so changing either server's output without
    changing the contract fails here.

    A shape that describes a single object rather than an array of them
    (``serverStatus``) is its own record schema; pass that one record in a
    one-element list.
    """
    shape = RESULT_SHAPES[shape_name]
    record_schema = shape["items"] if shape["type"] == "array" else shape

    for record in records:
        _assert_matches_object(record, record_schema, context)


def _assert_matches_object(record, schema: dict, context: str) -> None:
    """One object against one object schema, descending into nested ones.

    Nested rather than top-level-only because `nodeValues.engineering` is an
    object of objects: checking only that it *is* an object would let the two
    runtimes name its fields differently, which is the exact divergence this file
    exists to catch.
    """
    properties = schema["properties"]
    required = set(schema["required"])

    assert isinstance(record, dict), f"{context}: record is not an object: {record!r}"
    assert set(record) >= required, (
        f"{context}: record is missing {sorted(required - set(record))}: {record!r}"
    )
    if schema.get("additionalProperties") is False:
        assert set(record) <= set(properties), (
            f"{context}: record has fields outside the contract "
            f"{sorted(set(record) - set(properties))}: {record!r}"
        )
    for field, spec in properties.items():
        assert _matches_type(record[field], spec.get("type")), (
            f"{context}: {field}={record[field]!r} does not match "
            f"declared type {spec.get('type')!r}"
        )
        if isinstance(record[field], dict) and "properties" in spec:
            _assert_matches_object(record[field], spec, f"{context}.{field}")


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

    # 2) Description + schema parity per tool, compared *whole*.
    #
    #    This used to compare property names, the required set and each property's
    #    declared type, which is what let the real divergence through: the Python
    #    server derived its schema from the function signature, so every parameter
    #    description in the contract and every nested structure was missing from
    #    it — `write_opcua_nodes.nodes` advertised as "an array of object" against
    #    a contract naming `node_id`, `value` and the fifteen legal `data_type`
    #    spellings. Top-level names matched, so it passed. Both runtimes now
    #    advertise the contract's own schema, so the assertion is equality.
    for name, spec in EXPECTED.items():
        tool = advertised[name]
        assert tool.description == spec["description"], (
            f"{impl}/{name}: description differs from contract"
        )
        # `inputSchema` on the wire, `input_schema` on the SDK's model. Read the
        # attribute directly rather than via `getattr(..., {})`, so another rename
        # in the SDK fails here instead of quietly comparing against an empty set.
        assert tool.input_schema == _expected_schema(spec), (
            f"{impl}/{name}: advertised inputSchema differs from the contract's"
        )

        if shape_name := spec.get("resultShape"):
            assert tool.output_schema == {
                "type": "object",
                "properties": {"result": RESULT_SHAPES[shape_name]},
                "required": ["result"],
                "additionalProperties": False,
            }, f"{impl}/{name}: outputSchema differs from the shared result shape"


#: Tools this mock cannot exercise, with the reason. Named rather than omitted,
#: so "not covered here" stays a decision instead of an oversight.
UNEXERCISED = {
    # Needs a live condition instance to acknowledge; the alarms mock has one and
    # `test_events_e2e.py` drives it there.
    "acknowledge_alarm": "needs a live condition — covered by test_events_e2e.py",
    # The bundled mock implements no Alarms & Conditions, so ConditionRefresh
    # answers BadNothingToDo on *both* runtimes — identically, which is itself
    # parity, just not of a result shape. `test_events_e2e.py` runs it against
    # the alarms mock, where it returns records. Its shape is `eventRecords`,
    # which `read_events` exercises here.
    "list_active_alarms": "needs a server with Alarms & Conditions — see test_events_e2e.py",
}


def tool_calls(method_node_id: str) -> list[tuple[str, dict]]:
    """Arguments that exercise each tool against the bundled mock.

    Ordered so later tools have something to find: subscribing before listing,
    and subscribing to events before reading them. Every tool in the contract
    must appear here — `test_every_tool_is_exercised` fails otherwise — because a
    tool with a declared shape that nothing calls is a shape nothing checks,
    which is the state all seventeen tools were in before ten of them gained one.
    """
    return [
        ("get_server_status", {}),
        ("read_opcua_nodes", {"node_ids": [NODE["Temperature"], NODE["Pressure"]]}),
        ("browse_opcua_nodes", {"depth": 2, "include_values": True}),
        ("read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 3}),
        ("write_opcua_nodes", {"nodes": [{"node_id": NODE["ValvePosition"], "value": 42.5}]}),
        (
            "call_opcua_method",
            {"object_node_id": NODE["Methods"], "method_node_id": method_node_id},
        ),
        ("subscribe_opcua_nodes", {"node_ids": [NODE["Temperature"], NODE["Pressure"]]}),
        ("list_subscriptions", {}),
        ("subscribe_events", {}),
        ("read_events", {}),
        ("unsubscribe_opcua_nodes", {"subscription_ids": ["sub-1", "sub-2"]}),
    ]


def test_every_tool_is_exercised():
    """No tool may have a declared shape that nothing ever checks."""
    exercised = {name for name, _ in tool_calls("ns=2;i=0")} | set(UNEXERCISED)
    missing = set(EXPECTED) - exercised
    assert not missing, f"tools with a declared result shape but no parity call: {sorted(missing)}"


async def _stop_production_node_id(session) -> str:
    """The mock's StopProduction method, found rather than hardcoded.

    It takes no arguments and returns a Boolean, which makes it the cheapest
    method here to call for shape purposes.
    """
    browsed = await session.call_tool(
        "browse_opcua_nodes", {"node_id": NODE["Methods"], "node_class": "Method"}
    )
    assert not browsed.is_error, text_of(browsed)
    by_name = {
        node["browse_name"].split(":", 1)[1]: node["node_id"]
        for node in browsed.structured_content["result"]["nodes"]
    }
    assert "StopProduction" in by_name, f"methods found: {sorted(by_name)}"
    return by_name["StopProduction"]


async def test_every_tool_output_matches_its_declared_shape(impl_params):
    """Every tool's *actual* output, on both runtimes, against the contract.

    This is the systemic fix. Ten of the seventeen tools used to declare
    `resultShape: null`, and for those the output format, error wording and
    defaults were two hand-written copies that no test compared — the parity
    suite could prove the two servers *advertise* the same thing, never that they
    *do* the same thing. Four confirmed divergences lived in exactly that gap
    (#75, #76, browse formatting, and value stringification escaping the shared
    codec).

    With a shape on every tool, that gap is closed by construction: changing
    either server's output without changing the contract fails here.
    """
    impl, params = impl_params
    async with connect(params) as session:
        for name, arguments in tool_calls(await _stop_production_node_id(session)):
            spec = EXPECTED[name]
            shape_name = spec["resultShape"]
            result = await session.call_tool(name, arguments)
            assert not result.is_error, f"{impl}/{name}: {text_of(result)}"

            payload = result.structured_content
            assert payload is not None and "result" in payload, (
                f"{impl}/{name}: no structured content to check against {shape_name}"
            )
            body = payload["result"]
            records = body if isinstance(body, list) else [body]
            assert_matches_result_shape(records, shape_name, f"{impl}/{name}")

            # The compatibility text blocks must carry the same records, so a
            # client reading either one sees the same answer. `eventRecords` is
            # exempt: it may append a plain-text overflow notice, which is
            # deliberately not a record.
            if isinstance(body, list) and shape_name != "eventRecords":
                assert records_of(result) == body, (
                    f"{impl}/{name}: text blocks differ from structured content"
                )


async def test_a_browse_reports_whether_it_was_cut_short(impl_params):
    """Truncation is part of the answer, not a footnote.

    A browse that stopped early returns a *prefix*, which is indistinguishable
    from a complete result unless it says so — the same class of silent wrong
    answer as the missing continuation-point drain in #75. `nodeRefs` is an
    object rather than a bare array precisely so this flag has somewhere to live.
    """
    impl, params = impl_params
    async with connect(params) as session:
        complete = await session.call_tool("browse_opcua_nodes", {"depth": 3})
        clipped = await session.call_tool("browse_opcua_nodes", {"depth": 3, "max_nodes": 2})

    assert complete.structured_content["result"]["truncated"] is False, f"{impl}: {complete}"
    clipped_body = clipped.structured_content["result"]
    assert clipped_body["truncated"] is True, f"{impl}: a clipped browse must say so"
    assert clipped_body["inspected"] <= 2, f"{impl}: max_nodes was not honoured"


async def test_a_browse_path_resolves_to_the_same_node_id_on_both(impl_params):
    """Addressing by name must land on the node addressing by id does (issue #11)."""
    impl, params = impl_params
    async with connect(params) as session:
        resolved = await session.call_tool(
            "browse_opcua_nodes",
            {"browse_path": "/Objects/IndustrialControlSystem/Sensors/Temperature", "depth": 0},
        )
    assert not resolved.is_error, f"{impl}: {text_of(resolved)}"
    [node] = resolved.structured_content["result"]["nodes"]
    assert node["node_id"] == NODE["Temperature"], f"{impl}: resolved to {node['node_id']}"


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
            "subscribe_opcua_nodes",
            {"node_ids": [NODE["Temperature"]], "publishing_interval": 200, "buffer_size": 5},
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
