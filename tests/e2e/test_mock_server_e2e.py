"""Fidelity tests for the bundled mock OPC UA server itself.

The rest of the e2e suite drives the mock through the MCP servers, which is the
right altitude for nearly everything — but it cannot see whether the mock answers
a request the MCP servers never send. Both of them read a node before writing it,
so neither ever puts an unknown node id in a WriteRequest, and a mock that hangs
up on one looks perfectly healthy from up there (#64).

So these tests talk to the mock with a bare OPC UA client, at the level of the
service call, and assert the answers a conformant server owes a client that is
not as careful as ours.

Run:
    cd tests && uv run pytest -v e2e/test_mock_server_e2e.py
"""

from __future__ import annotations

from opcua import Client, ua

# ValvePosition: a writable Double in the mock's address space, and a node id it
# has no chance of having. Same pair the batch-tool tests in test_mcp_e2e.py use.
VALVE_POSITION = "ns=2;i=13"
UNKNOWN_NODE = "ns=2;i=999999"


def _write_double(node_id: str, value: float) -> ua.WriteValue:
    item = ua.WriteValue()
    item.NodeId = ua.NodeId.from_string(node_id)
    item.AttributeId = ua.AttributeIds.Value
    item.Value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    return item


def test_a_write_to_an_unknown_node_is_answered_per_item(opcua_server):
    """One unknown node id in a WriteRequest costs that item, not the batch.

    python-opcua's server used to raise out of its request handler on this (it
    bit-tests an AccessLevel it never read, because the node does not exist),
    answer nothing at all and close the connection. A client then sat on the
    request until its own transaction timeout — 15s in node-opcua — and lost the
    writes that *did* land alongside it: the mock applies them before it trips.
    `answer_writes_to_unknown_nodes` in the mock screens those ids out first.

    The good node goes first on purpose: a server that answered the unknown item
    but dropped the rest would still line up if the statuses were checked as a
    set rather than in order.
    """
    client = Client(opcua_server, timeout=10)
    client.connect()
    try:
        params = ua.WriteParameters()
        params.NodesToWrite = [
            _write_double(VALVE_POSITION, 42.5),
            _write_double(UNKNOWN_NODE, 1.0),
        ]
        statuses = client.uaclient.write(params)

        # Good is the server saying it applied that write — the half of the batch
        # the timeout used to swallow. That the value lands too is pinned one
        # level up, by `test_batch_write_keeps_valid_items_when_one_node_is_
        # rejected` in test_mcp_e2e.py; it is not asserted here because the
        # simulation loop drives ValvePosition back from `system_state` on its
        # next tick, and racing that is not what this test is about.
        assert [status.value for status in statuses] == [
            ua.StatusCodes.Good,
            ua.StatusCodes.BadNodeIdUnknown,
        ], f"unexpected per-item statuses: {[str(s) for s in statuses]}"
    finally:
        client.disconnect()


def test_the_session_survives_a_write_to_an_unknown_node(opcua_server):
    """The connection stays usable afterwards.

    The failure this pins is not a status code but a dropped TCP connection: the
    exception escaping the handler took the whole session with it, so the next
    request on it failed too. A conformant server rejects the item and carries on.
    """
    client = Client(opcua_server, timeout=10)
    client.connect()
    try:
        params = ua.WriteParameters()
        params.NodesToWrite = [_write_double(UNKNOWN_NODE, 1.0)]
        [status] = client.uaclient.write(params)
        assert status.value == ua.StatusCodes.BadNodeIdUnknown, str(status)

        temperature = client.get_node("ns=2;i=3").get_value()
        assert isinstance(temperature, float), f"session unusable afterwards: {temperature!r}"
    finally:
        client.disconnect()
