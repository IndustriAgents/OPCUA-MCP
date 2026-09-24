"""Diff the two runtimes' output against *each other*, not against the contract.

`test_contract_parity.py` is parameterised per implementation: it checks Node
against the contract, then Python against the contract. It never compares one
runtime's answer with the other's. That is a real gap, and it is the gap the
0.3.0 architecture review found four bugs in — wherever the contract is silent or
loose, both runtimes pass while disagreeing.

A declared `resultShape` on every tool closes most of it. This closes the rest:
the same server, the same tool, the same arguments, and the two structured
results must be equal. A shape loose enough to admit `true` and `True`, or
`[high, low]` and `9007199254740993`, is caught here rather than in production.

Fields that *cannot* be equal are masked rather than dropped: a timestamp differs
because the two calls happen at different moments, and a sensor value differs
because the mock's Temperature moves once a second. Masking keeps the field's
presence and type under test while removing only the part that is legitimately
different — dropping it would quietly stop checking that the field is there at
all.

Run as part of the normal suite:
    uv sync --all-packages && cd tests && uv run --no-sync pytest -v
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, text_of

#: Fields whose value legitimately differs between two calls a moment apart.
#:
#: Replaced with a marker of the same shape rather than removed, so the field
#: still has to be present, and still has to be null exactly when the other
#: runtime reports it as null.
VOLATILE_FIELDS = {
    "value",  # the mock's sensors move on their own
    "source_timestamp",
    "server_timestamp",
    "timestamp",
    "current_time",
    "start_time",
    "change_count",
    "changes",
    "subscription_id",  # per-process counter, not a property of the node
    "event_id",
    "time",
    "receive_time",
    "inspected",  # depends on which nodes a browse happened to reach first
}


def mask(value: Any, *, inside_volatile: bool = False) -> Any:
    """`value` with volatile leaves replaced by a marker of the same nullness.

    `None` survives as `None`: a field one runtime reports as null and the other
    as a value is a real divergence, and masking must not hide it.
    """
    if isinstance(value, dict):
        return {
            key: mask(item, inside_volatile=inside_volatile or key in VOLATILE_FIELDS)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask(item, inside_volatile=inside_volatile) for item in value]
    if inside_volatile:
        return None if value is None else "<volatile>"
    return value


@pytest.fixture
def both(opcua_server):
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built — run `npm install && npm run build`")
    return {impl: _server_params(impl, opcua_server) for impl in ("python", "node")}


async def call_on_both(both, name: str, arguments: dict) -> dict[str, Any]:
    """Run one tool on each runtime and return their structured results."""
    results = {}
    for impl, params in both.items():
        async with connect(params) as session:
            result = await session.call_tool(name, arguments)
            assert not result.is_error, f"{impl}/{name}: {text_of(result)}"
            assert result.structured_content is not None, f"{impl}/{name}: no structured content"
            results[impl] = result.structured_content["result"]
    return results


# The calls compared. Deliberately the read-shaped tools: those are where the
# encoding lives, and where the divergences the review found actually were.
DIFFERENTIAL_CALLS = [
    ("read_opcua_nodes", {"node_ids": [NODE["Temperature"], NODE["PumpEnabled"]]}),
    ("read_opcua_nodes", {"node_ids": [NODE["SystemMode"], NODE["ProductionRate"]]}),
    ("read_opcua_nodes", {"node_ids": ["ns=2;i=999999"]}),  # a rejected node
    ("browse_opcua_nodes", {}),
    ("browse_opcua_nodes", {"depth": 2, "node_class": "Variable"}),
    ("browse_opcua_nodes", {"depth": 2, "name_filter": "temp"}),
    (
        "browse_opcua_nodes",
        {"browse_path": "/Objects/IndustrialControlSystem/Sensors", "depth": 1},
    ),
    ("read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 2}),
    ("write_opcua_nodes", {"nodes": [{"node_id": NODE["ValvePosition"], "value": 12.5}]}),
    (
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["PumpEnabled"], "value": True, "data_type": "Boolean"}]},
    ),
    # A batch mixing a writable node with one the server does not have: the
    # per-node statuses must agree, which is the case #76 was about.
    (
        "write_opcua_nodes",
        {
            "nodes": [
                {"node_id": NODE["ValvePosition"], "value": 1.5},
                {"node_id": "ns=2;i=999999", "value": 1},
            ]
        },
    ),
    ("subscribe_events", {}),
    # Values the write codec must refuse identically (#157), each for a reason
    # one runtime used to write anyway: Node's Number() read "0x10" as 16, Python's
    # float() read true as 1.0, and a zone-less DateTime was local time on one
    # runtime and UTC on the other. Every one fails conversion, so nothing is sent.
    (
        "write_opcua_nodes",
        {
            "nodes": [
                {"node_id": NODE["ScratchDouble"], "value": "0x10"},
                {"node_id": NODE["ScratchDouble"], "value": True},
                {"node_id": NODE["ScratchDouble"], "value": [5]},
                {
                    "node_id": NODE["ScratchDouble"],
                    "value": "2026-04-23T17:40:00",
                    "data_type": "DateTime",
                },
            ]
        },
    ),
    # A method argument declared as Duration (i=290): a Double on the wire. Node
    # used to refuse the call and Python to guess every argument; the mock echoes
    # what it received, so `outputs` shows both now send the same Double.
    (
        "call_opcua_method",
        {
            "object_node_id": NODE["Methods"],
            "method_node_id": NODE["EchoDuration"],
            "arguments": [1500],
        },
    ),
    (
        "call_opcua_method",
        {
            "object_node_id": NODE["Methods"],
            "method_node_id": NODE["EchoDuration"],
            "arguments": ["1500"],
        },
    ),
]


#: Calls whose `completeness` must be identical on both, not merely present.
#: Chosen so nothing in it is volatile: a backward history read cannot be
#: resumed, so its continuation is null on both, and a browse's counts depend
#: only on the address space.
COMPLETENESS_CALLS = [
    ("read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 2}),
    ("browse_opcua_nodes", {"depth": 3}),
    ("browse_opcua_nodes", {"depth": 3, "max_nodes": 2}),
    ("list_subscriptions", {}),
]


@pytest.mark.parametrize(
    ("name", "arguments"),
    COMPLETENESS_CALLS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(COMPLETENESS_CALLS)],
)
async def test_both_runtimes_say_the_same_about_what_is_missing(both, name, arguments):
    """Two servers that return the same records must also agree on whether they are all."""
    answers = {}
    for impl, params in both.items():
        async with connect(params) as session:
            result = await session.call_tool(name, arguments)
        assert not result.is_error, f"{impl}/{name}: {text_of(result)}"
        answers[impl] = result.structured_content["completeness"]
    assert answers["python"] == answers["node"], (
        f"{name}: python says {answers['python']}, node says {answers['node']}"
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    DIFFERENTIAL_CALLS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(DIFFERENTIAL_CALLS)],
)
async def test_both_runtimes_return_the_same_record(both, name, arguments):
    """The two servers must answer the same question with the same document."""
    results = await call_on_both(both, name, arguments)
    assert mask(results["python"]) == mask(results["node"]), (
        f"{name} diverges between runtimes:\n"
        f"python: {json.dumps(results['python'], indent=2, sort_keys=True)}\n"
        f"node:   {json.dumps(results['node'], indent=2, sort_keys=True)}"
    )


async def test_the_same_value_is_encoded_the_same_way_by_both(both):
    """The finding that motivated this file.

    `read_opcua_node` stringified natively on both runtimes and so diverged by
    construction: a Boolean rendered `true` against `True`, and an Int64 came
    back as node-opcua's `[high, low]` pair against a plain int. The shared codec
    that `tests/fixtures/value-encoding.json` pins covered only `records.*`,
    which is to say the history family — everything the *read* path produced was
    outside its reach.

    Values, not just shapes, so `mask` is deliberately not used here. The nodes
    chosen are the stable ones the mock does not move on its own.
    """
    results = await call_on_both(
        both,
        "read_opcua_nodes",
        {"node_ids": [NODE["PumpEnabled"], NODE["SystemMode"], NODE["ValvePosition"]]},
    )
    python_values = {record["node_id"]: record["value"] for record in results["python"]}
    node_values = {record["node_id"]: record["value"] for record in results["node"]}
    assert python_values == node_values

    python_types = {record["node_id"]: record["data_type"] for record in results["python"]}
    node_types = {record["node_id"]: record["data_type"] for record in results["node"]}
    assert python_types == node_types
    # A Boolean must be a JSON boolean on both, not 1/0 and not "True".
    assert isinstance(python_values[NODE["PumpEnabled"]], bool)
    assert python_types[NODE["PumpEnabled"]] == "Boolean"


async def test_a_good_subcode_is_a_value_on_both(both):
    """GoodLocalOverride is a success that says something more (#157).

    Node compared against plain Good and reported this reading as null; Python
    tested severity and returned it. Both now return the value and name the
    subcode, so the override is visible rather than either hidden or mistaken
    for a failure. Unmasked: the mock never moves this node.
    """
    results = await call_on_both(
        both, "read_opcua_nodes", {"node_ids": [NODE["OverriddenSetpoint"]]}
    )
    for impl, records in results.items():
        [record] = records
        assert record["value"] == 42.5, f"{impl}: {record}"
        assert record["data_type"] == "Double", f"{impl}: {record}"
        assert record["status"] == "GoodLocalOverride", f"{impl}: {record}"


async def test_a_duration_argument_reaches_the_plant_as_the_same_double(both):
    """What the method received, not only that it was called (#157)."""
    results = await call_on_both(
        both,
        "call_opcua_method",
        {
            "object_node_id": NODE["Methods"],
            "method_node_id": NODE["EchoDuration"],
            "arguments": ["1500"],
        },
    )
    for impl, record in results.items():
        assert record["outputs"] == ["Double:1500.0"], f"{impl}: {record}"
        assert record["status"] == "Good", f"{impl}: {record}"


async def test_a_rejected_node_is_reported_the_same_way_by_both(both):
    """The status name and the nulls around it, not just "an error happened".

    Both runtimes must name the OPC UA status identically and must agree that
    the value and its type are null — the shape permits a runtime to report
    either as something else, and this is where that would show.
    """
    results = await call_on_both(both, "read_opcua_nodes", {"node_ids": ["ns=2;i=999999"]})
    for impl, records in results.items():
        [record] = records
        assert record["value"] is None, impl
        assert record["data_type"] is None, impl
        assert record["status"].startswith("Bad"), f"{impl}: {record['status']}"
    assert results["python"][0]["status"] == results["node"][0]["status"]


# Failing calls, and the whole sentence each must be refused with.
#
# This is the half of the surface that had no differential coverage at all. The
# successful results are pinned by `resultShapes`, so the shapes could not drift;
# nothing pinned a failure, and the framing had drifted years ago — the Node
# runtime prefixed every message with "Error: " and the Python one did not, so a
# model saw a different sentence depending on which runtime its client had
# started. The old version of this test compared *substrings* for exactly that
# reason, which is what let the prefix survive being looked at.
#
# `contract/tools.json` -> `errors` now words all of it, so the assertion is
# equality on the full text. Only messages this server composes are here: where a
# client library's own rejection is interpolated (`{reason}`), the two libraries
# legitimately word the same OPC UA status differently.
DIFFERENTIAL_FAILURES = [
    (
        "a browse path that does not resolve",
        "browse_opcua_nodes",
        {"browse_path": "/Objects/NoSuchThing"},
        'browse_path "/Objects/NoSuchThing" does not resolve: no child "NoSuchThing" '
        "under ns=0;i=85",
    ),
    (
        "a required argument left out",
        "read_opcua_nodes",
        {},
        "read_opcua_nodes requires node_ids",
    ),
    (
        "an empty batch read",
        "read_opcua_nodes",
        {"node_ids": []},
        "read_opcua_nodes requires a non-empty node_ids array",
    ),
    (
        "one node id sent bare instead of as a list",
        "read_opcua_nodes",
        {"node_ids": NODE["Temperature"]},
        "read_opcua_nodes argument node_ids must be an array of strings",
    ),
    (
        "a write entry with no value",
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ValvePosition"]}]},
        "write_opcua_nodes requires nodes[0].value",
    ),
    (
        "a write batch sent as one object",
        "write_opcua_nodes",
        {"nodes": {"node_id": NODE["ValvePosition"], "value": 1}},
        "write_opcua_nodes argument nodes must be an array of objects",
    ),
    (
        "a depth that is not a number",
        "browse_opcua_nodes",
        {"depth": "deep"},
        "browse_opcua_nodes argument depth must be an integer",
    ),
    (
        "an aggregate read with no start time",
        "read_opcua_history",
        {"node_id": NODE["Temperature"], "aggregate_function": "Average"},
        "read_opcua_history requires start_time when aggregate_function is given",
    ),
    (
        # #157: Node read this as the host's local time and Python as UTC — two
        # different windows of history for one call. Refused on both instead.
        "a history start time with no timezone",
        "read_opcua_history",
        {"node_id": NODE["Temperature"], "start_time": "2026-04-23T17:40:00"},
        f"Failed to read history of node {NODE['Temperature']}: "
        'Invalid date/time: "2026-04-23T17:40:00" has no timezone, so the instant it names '
        "depends on where it is read. Add Z for UTC or an offset such as +02:00, "
        "e.g. 2026-04-23T17:40:00Z",
    ),
    (
        # The same refusal, and on Python no longer an SDK crash: the date was
        # parsed outside the handler that words refusals.
        "an event history end time with no timezone",
        "read_event_history",
        {"end_time": "2026-04-23T17:40:00"},
        'Invalid date/time: "2026-04-23T17:40:00" has no timezone, so the instant it names '
        "depends on where it is read. Add Z for UTC or an offset such as +02:00, "
        "e.g. 2026-04-23T17:40:00Z",
    ),
    (
        # #157: a method publishing no InputArguments had its array argument sent
        # as the string "1,2" by Node and as an Int64 array by Python. Refused on
        # both before anything is sent — the mock's StopProduction would
        # otherwise run.
        "an array argument to a method that declares no argument types",
        "call_opcua_method",
        {
            "object_node_id": NODE["Methods"],
            "method_node_id": "ns=2;i=31",
            "arguments": [[1, 2]],
        },
        f"Failed to call method ns=2;i=31 on object {NODE['Methods']}: arguments[0] is an "
        "array, and the method publishes no InputArguments to say what type it expects; "
        "pass a boolean, a number or a string",
    ),
    (
        "cancelling a subscription that was never made",
        "unsubscribe_opcua_nodes",
        {"subscription_ids": ["sub-does-not-exist"]},
        "No such subscription: sub-does-not-exist",
    ),
    (
        "cancelling several subscriptions that were never made",
        "unsubscribe_opcua_nodes",
        {"subscription_ids": ["sub-a", "sub-b"]},
        "No such subscriptions: sub-a, sub-b. Nothing was cancelled.",
    ),
    (
        "an empty cancel",
        "unsubscribe_opcua_nodes",
        {"subscription_ids": []},
        "unsubscribe_opcua_nodes requires a non-empty subscription_ids array",
    ),
    (
        "reading events without subscribing first",
        "read_events",
        {"node_id": "ns=0;i=2253"},
        "Not subscribed to events from node ns=0;i=2253. Call subscribe_events first.",
    ),
    (
        "acknowledging an alarm nobody reported",
        "acknowledge_alarm",
        {"event_id": "nope"},
        'Unknown event_id "nope". Call list_active_alarms first, or pass the condition_id '
        "of the alarm to acknowledge.",
    ),
    (
        "a tool that is not in the contract",
        "read_opcua_tags",
        {},
        "Unknown tool: read_opcua_tags",
    ),
    (
        "a batch read past the per-call maximum",
        "read_opcua_nodes",
        {"node_ids": [f"ns=2;i={index}" for index in range(501)]},
        "read_opcua_nodes accepts at most 500 entries in node_ids, got 501. Nothing was sent "
        "to the OPC UA server; split the request.",
    ),
    # Issue #139: every other request shape, refused before anything is sent.
    (
        "a write batch past the per-call maximum",
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ScratchDouble"], "value": 1.0}] * 101},
        "write_opcua_nodes accepts at most 100 entries in nodes, got 101. Nothing was sent to "
        "the OPC UA server; split the request.",
    ),
    (
        "a write batch past the OPC UA server's own MaxNodesPerWrite",
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ScratchDouble"], "value": 1.0}] * 51},
        "write_opcua_nodes was asked to write 51 nodes, but the OPC UA server accepts at most 50 "
        "in one Write (its OperationLimits.MaxNodesPerWrite). Nothing was written. Split the "
        "request: a write is not split here, because each part could land or fail on its own.",
    ),
    (
        "a method call with more arguments than any method takes",
        "call_opcua_method",
        {"object_node_id": NODE["Methods"], "method_node_id": "ns=2;i=31", "arguments": [1] * 65},
        "call_opcua_method accepts at most 64 entries in arguments, got 65. Nothing was sent to "
        "the OPC UA server; split the request.",
    ),
    (
        "a string past the per-string maximum",
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ScratchDouble"], "value": "a" * 131073}]},
        "write_opcua_nodes argument nodes[0].value is a string of 131073 bytes, over the "
        "131072-byte limit on one string (limits.maxStringBytes). Nothing was sent to the "
        "OPC UA server.",
    ),
    (
        "a value nested past the depth limit",
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ScratchDouble"], "value": [[[[[[1.0]]]]]]}]},
        "write_opcua_nodes argument nodes[0].value[0][0][0][0][0] nests arrays or objects more "
        "than 8 levels deep, counting the arguments themselves (limits.maxNestingDepth). "
        "Nothing was sent to the OPC UA server.",
    ),
    (
        "an array value past the per-array maximum",
        "write_opcua_nodes",
        {"nodes": [{"node_id": NODE["ScratchDouble"], "value": [0] * 10001}]},
        "write_opcua_nodes argument nodes[0].value has 10001 items, over the 10000-item limit "
        "on one array (limits.maxArrayItems). Nothing was sent to the OPC UA server.",
    ),
    (
        "a ByteString past its maximum once decoded",
        "write_opcua_nodes",
        {
            "nodes": [
                {
                    "node_id": NODE["ScratchDouble"],
                    "value": base64.b64encode(bytes(65537)).decode("ascii"),
                    "data_type": "ByteString",
                }
            ]
        },
        "A ByteString of 65537 bytes is over the 65536-byte limit on one ByteString value "
        "(limits.maxByteStringBytes). Nothing was sent to the OPC UA server.",
    ),
    (
        "subscribing to more nodes than this server will hold",
        "subscribe_opcua_nodes",
        {"node_ids": [f"ns=2;i={index}" for index in range(201)]},
        "This server already holds 0 of at most 200 subscriptions; subscribing to 201 more "
        "would exceed it. Cancel some with unsubscribe_opcua_nodes.",
    ),
]


@pytest.mark.parametrize(
    ("name", "tool", "arguments", "expected"),
    DIFFERENTIAL_FAILURES,
    ids=[case[0] for case in DIFFERENTIAL_FAILURES],
)
async def test_both_runtimes_word_the_same_failure_identically(
    both, name, tool, arguments, expected
):
    """Both servers must refuse the same call with the same sentence, exactly."""
    failures = {}
    for impl, params in both.items():
        async with connect(params) as session:
            result = await session.call_tool(tool, arguments)
        assert result.is_error, f"{impl}: {name} must fail, got {text_of(result)!r}"
        failures[impl] = text_of(result).strip()

    assert failures["python"] == failures["node"], (
        f"{name} is worded differently by the two runtimes:\n"
        f"python: {failures['python']!r}\n"
        f"node:   {failures['node']!r}"
    )
    assert failures["python"] == expected, (
        f"{name}: both runtimes say {failures['python']!r}, contract says {expected!r}"
    )


def test_every_tool_family_is_represented():
    """A differential suite that drifts behind the contract stops being one.

    Not every tool — some are inherently per-process (`get_server_status`) or
    need a server this mock is not (`list_active_alarms`). But every tool whose
    output is a *reading of the address space* has to be here, because that is
    what the two client libraries represent differently.
    """
    compared = {name for name, _ in DIFFERENTIAL_CALLS}
    required = {
        "read_opcua_nodes",
        "browse_opcua_nodes",
        "read_opcua_history",
        "write_opcua_nodes",
    }
    assert required <= compared, f"not compared across runtimes: {sorted(required - compared)}"
