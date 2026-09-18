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

import time

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


#: A writable actuator the simulation republishes once a second, and a writable
#: node it never touches. `tests/e2e/test_mcp_e2e.py` keeps the same map.
ACTUATOR_NODE_ID = "ns=2;i=13"
SCRATCH_DOUBLE_NODE_ID = "ns=2;i=41"


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


def test_the_simulation_reverts_an_actuator_but_leaves_the_scratch_nodes_alone(opcua_server):
    """The mock's own contract, pinned — because a test bet against it and lost.

    Every actuator here is republished from the simulation's state once a second.
    That is realistic and deliberate: the README tells people their writes to
    those nodes are transient. It also means a test that writes an actuator and
    reads it back is racing a one-second timer, which passes locally, passes in
    CI, and then fails a release verify — which is exactly what
    `test_batch_write_keeps_valid_items_when_one_node_is_rejected` did, reading
    back 50.0 where it had written 31.5.

    The `Scratch` nodes exist so write-then-read-back has somewhere safe to live.
    This test states both halves, so that re-pointing a write test at an actuator
    fails *here*, with an explanation, rather than intermittently somewhere else.
    """
    client = Client(opcua_server)
    client.connect()
    try:
        actuator = client.get_node(ACTUATOR_NODE_ID)
        scratch = client.get_node(SCRATCH_DOUBLE_NODE_ID)
        for node in (actuator, scratch):
            node.set_value(ua.Variant(31.5, ua.VariantType.Double))
            assert node.get_value() == 31.5, "the write did not land at all"

        # Longer than the simulation's one-second period, so this is not a race
        # in the other direction.
        time.sleep(1.8)

        assert actuator.get_value() != 31.5, (
            "an actuator kept a written value; if the mock stopped republishing "
            "them, this test and the comments pointing at it are now misleading"
        )
        assert scratch.get_value() == 31.5, (
            "the scratch node was overwritten — something now simulates it, and "
            "every write-then-read-back test in the suite has become a race"
        )
    finally:
        client.disconnect()
