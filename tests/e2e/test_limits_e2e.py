"""End to end: request bounds (issue #139) and machine-readable partial results (#137).

Both runtimes, against the main mock, which publishes OperationLimits of 100
nodes per Read and 50 per Write — deliberately below the MCP servers' own caps
(500 and 100) — so the path where the plant's stated limit is the one that binds
runs here, not only in a unit test. See packages/mock-server/README.md.

The wording of each refusal is compared across the two runtimes in
``test_runtime_differential.py``; what is asserted here is behaviour: that a
refused write reaches nothing, that a chunked read comes back whole and in
order, and that a result cut short says so in ``structuredContent`` — not only in
a trailing sentence a client reading the structured result never sees.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from test_mcp_e2e import (
    NODE,
    NODE_BUILD,
    UNKNOWN_NODE,
    _server_params,
    connect,
    records_of,
    text_of,
)

#: What the mock publishes; packages/mock-server/opcua_local_server.py.
MOCK_MAX_NODES_PER_READ = 100
MOCK_MAX_NODES_PER_WRITE = 50

COMPLETENESS_FIELDS = {
    "complete",
    "reasons",
    "returned",
    "truncated",
    "limit",
    "dropped",
    "remaining",
    "continuation",
}


@pytest.fixture(params=["python", "node"])
def server(request, opcua_server):
    """``(impl_name, StdioServerParameters)`` against the main mock."""
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, opcua_server)


def iso_utc(offset_seconds: float = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return moment.isoformat().replace("+00:00", "Z")


def completeness_of(result) -> dict:
    """The structured completeness object, checked for its declared fields."""
    assert result.structured_content is not None, "no structured content"
    completeness = result.structured_content.get("completeness")
    assert isinstance(completeness, dict), f"no completeness beside result: {result}"
    assert set(completeness) == COMPLETENESS_FIELDS, sorted(completeness)
    return completeness


# --- #139: request bounds ------------------------------------------------------


async def test_an_oversized_write_batch_is_refused_and_reaches_nothing(server):
    """Over the contract's cap and over the server's: both refused, nothing written.

    The second batch is within this server's own 100 but over the 50 the mock
    publishes. Splitting it would have sent two Writes; refusing it sends none,
    which is what the read-back proves.
    """
    impl, params = server
    sentinel, attempted = 11.25, 99.5
    async with connect(params) as session:
        primed = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": NODE["ScratchDouble"], "value": sentinel}]}
        )
        assert not primed.is_error, text_of(primed)

        write = {"node_id": NODE["ScratchDouble"], "value": attempted}
        over_contract = await session.call_tool("write_opcua_nodes", {"nodes": [write] * 101})
        over_server = await session.call_tool(
            "write_opcua_nodes", {"nodes": [write] * (MOCK_MAX_NODES_PER_WRITE + 1)}
        )
        at_server = await session.call_tool(
            "write_opcua_nodes", {"nodes": [write] * MOCK_MAX_NODES_PER_WRITE}
        )
        after = records_of(
            await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchDouble"]]})
        )
        # Put it back for whoever reads it next.
        await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": NODE["ScratchDouble"], "value": 0.0}]}
        )

    assert over_contract.is_error, f"{impl}: 101 writes were accepted"
    assert "accepts at most 100 entries in nodes, got 101" in text_of(over_contract), impl

    assert over_server.is_error, f"{impl}: a batch over the server's MaxNodesPerWrite was sent"
    assert "OperationLimits.MaxNodesPerWrite" in text_of(over_server), text_of(over_server)
    assert f"accepts at most {MOCK_MAX_NODES_PER_WRITE}" in text_of(over_server)

    # Exactly the server's limit is one Write, and it lands.
    assert not at_server.is_error, text_of(at_server)
    assert [record["status"] for record in records_of(at_server)] == [
        "Good"
    ] * MOCK_MAX_NODES_PER_WRITE
    assert after[0]["value"] == attempted, (
        f"{impl}: read back {after[0]['value']}, so the refused batches reached the plant "
        f"or the accepted one did not"
    )


async def test_a_read_over_the_servers_limit_is_chunked_and_comes_back_in_order(server):
    """150 nodes against a server that takes 100 per Read: two Reads, one answer.

    Mixed with a node the server does not have, so each record's status has to
    come back in its own place — a chunking that reordered, or dropped the second
    request, would put a Bad status on the wrong node or return 100 records.
    """
    impl, params = server
    asked = [NODE["Temperature"], UNKNOWN_NODE, NODE["Pressure"]] * 50
    assert len(asked) > MOCK_MAX_NODES_PER_READ
    async with connect(params) as session:
        result = await session.call_tool("read_opcua_nodes", {"node_ids": asked})
    assert not result.is_error, text_of(result)
    records = result.structured_content["result"]
    assert [record["node_id"] for record in records] == asked, f"{impl}: order was not kept"
    assert [record["status"] for record in records] == [
        "Good",
        "BadNodeIdUnknown",
        "Good",
    ] * 50, f"{impl}: a status landed on the wrong node"


# --- #137: partial results in structuredContent ------------------------------------


async def test_a_truncated_history_read_says_so_in_structured_content(server):
    """num_values reached, with more in the range: partial, and where to resume.

    A forward read — start_time given — so the continuation can be followed, and
    following it must return the rest: records at or after the boundary, the
    boundary record itself repeating rather than being skipped.
    """
    impl, params = server
    window = {
        "node_id": NODE["Temperature"],
        "start_time": iso_utc(3600),
        "end_time": iso_utc(-60),
        "num_values": 2,
    }
    async with connect(params) as session:
        first = await session.call_tool("read_opcua_history", window)
        assert not first.is_error, text_of(first)
        completeness = completeness_of(first)
        continuation = completeness["continuation"]
        assert continuation, f"{impl}: a truncated forward read gave nowhere to resume"
        second = await session.call_tool("read_opcua_history", {**window, **continuation})

    records = first.structured_content["result"]
    assert len(records) == 2, records
    assert completeness["complete"] is False, f"{impl}: {completeness}"
    assert completeness["reasons"] == ["requestLimit"], completeness
    assert completeness["truncated"] is True and completeness["limit"] == 2, completeness
    assert completeness["returned"] == 2, completeness
    assert completeness["dropped"] == 0, completeness
    # The mock holds more than two readings and says so with a continuation point.
    assert completeness["remaining"] is True, completeness
    assert continuation == {"start_time": records[-1]["timestamp"]}, continuation

    # A caller that asked for two and got two is not told it may be missing
    # something: the structured result says so, and the text stays records.
    assert records_of(first) == records, f"{impl}: a notice was appended to a requested cap"

    resumed = second.structured_content["result"]
    assert not second.is_error and resumed, f"{impl}: following the continuation found nothing"
    boundary = _instant(records[-1]["timestamp"])
    assert all(_instant(record["timestamp"]) >= boundary for record in resumed), (
        f"{impl}: the continuation went backwards: {records[-1]} then {resumed}"
    )


async def test_a_backward_history_read_is_partial_but_cannot_be_resumed(server):
    """No start_time reads newest first, and no start_time can ask for older."""
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "read_opcua_history", {"node_id": NODE["Temperature"], "num_values": 2}
        )
    assert not result.is_error, text_of(result)
    completeness = completeness_of(result)
    assert completeness["complete"] is False and completeness["truncated"] is True, completeness
    assert completeness["continuation"] is None, f"{impl}: {completeness}"


async def test_a_history_read_that_got_everything_says_it_is_complete(server):
    """A range with nothing in it is a complete answer, not an unknown one."""
    impl, params = server
    async with connect(params) as session:
        result = await session.call_tool(
            "read_opcua_history",
            {
                "node_id": NODE["Temperature"],
                "start_time": "2020-01-01T00:00:00Z",
                "end_time": "2020-01-01T01:00:00Z",
            },
        )
    assert not result.is_error, text_of(result)
    completeness = completeness_of(result)
    assert completeness == {
        "complete": True,
        "reasons": [],
        "returned": 0,
        "truncated": False,
        "limit": None,
        "dropped": 0,
        "remaining": False,
        "continuation": None,
    }, f"{impl}: {completeness}"


async def test_a_subscription_buffer_that_dropped_changes_says_so(server):
    """A ring of one, several changes: each record and the whole result say how many went."""
    impl, params = server
    async with connect(params) as session:
        created = await session.call_tool(
            "subscribe_opcua_nodes",
            {"node_ids": [NODE["Temperature"]], "publishing_interval": 200, "buffer_size": 1},
        )
        assert not created.is_error, text_of(created)
        subscription_id = created.structured_content["result"][0]["subscription_id"]
        listed = None
        for _ in range(12):
            listed = await session.call_tool("list_subscriptions", {})
            [record] = [
                entry
                for entry in listed.structured_content["result"]
                if entry["subscription_id"] == subscription_id
            ]
            if record["change_count"] >= 3:
                break
            await asyncio.sleep(1)
        cancelled = await session.call_tool(
            "unsubscribe_opcua_nodes", {"subscription_ids": [subscription_id]}
        )

    assert record["change_count"] >= 3, f"{impl}: the sensor never moved: {record}"
    assert len(record["changes"]) == 1, record
    assert record["dropped"] == record["change_count"] - 1, f"{impl}: {record}"

    completeness = completeness_of(listed)
    assert completeness["complete"] is False, f"{impl}: {completeness}"
    assert completeness["reasons"] == ["bufferOverflow"], completeness
    assert completeness["dropped"] == record["dropped"], completeness
    assert completeness["truncated"] is False and completeness["remaining"] is False

    # The final look at a cancelled subscription carries the same account.
    final = completeness_of(cancelled)
    assert final["dropped"] >= record["dropped"] and final["complete"] is False, final


async def test_the_advertised_output_schema_carries_completeness(server):
    """What a client validates against must describe what it is sent."""
    impl, params = server
    async with connect(params) as session:
        listed = await session.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    for name in ("read_opcua_history", "browse_opcua_nodes", "read_events", "list_subscriptions"):
        schema = tools[name].output_schema
        assert schema["required"] == ["result", "completeness"], f"{impl}/{name}: {schema}"
        assert set(schema["properties"]["completeness"]["properties"]) == COMPLETENESS_FIELDS
    # A tool that cannot be partial does not grow the field.
    assert "completeness" not in tools["read_opcua_nodes"].output_schema["properties"]


def _instant(timestamp: str) -> datetime:
    """An ISO-8601 UTC timestamp as an instant, whatever its sub-second precision."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
