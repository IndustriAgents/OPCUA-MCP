"""What a browsed node *is*, not just what class it belongs to (issue #120).

A browse record named a node, its class and its parent. That is enough to walk
an address space and not enough to understand one: every alarm, every pump and
every folder came back as ``Object``, and every reading came back as
``Variable``. The information was already there — OPC UA models it as a
``HasTypeDefinition`` reference — and nothing read it.

It takes a second browse, because ``HasTypeDefinition`` is non-hierarchical and
the traversal's own browse asks for forward hierarchical references only (on
purpose: browsing everything makes a node answer with its parent and its type
instead of its children). So the thing worth asserting end to end is that the
extra browse actually resolves against a real server, that both runtimes resolve
it identically, and that it costs one batched request rather than one per node.

Two mocks, because they demonstrate different halves:

* the **main** mock has ``ScratchAnalog`` (``ns=2;i=90``), a real
  ``AnalogItemType`` — which is the type that says "this node publishes a unit
  and a range", so it composes with #110;
* the **alarms** mock has a real ``ExclusiveLimitAlarmType``, which is the case
  the class alone cannot express at all: it and the folder above it are both
  ``Object``.

Run:
    cd tests && uv run --no-sync pytest e2e/test_type_definitions_e2e.py -v
"""

from __future__ import annotations

import time

import pytest
from test_mcp_e2e import NODE, NODE_BUILD, _server_params, connect, text_of


@pytest.fixture(params=["python", "node"])
def server(request, opcua_server):
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, opcua_server)


@pytest.fixture(params=["python", "node"])
def alarm_server(request, alarm_opcua_server):
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return impl, _server_params(impl, alarm_opcua_server)


async def browse(session, arguments: dict) -> dict:
    """``{node_id: record}`` for one browse, asserting it succeeded."""
    result = await session.call_tool("browse_opcua_nodes", arguments)
    assert not result.is_error, text_of(result)
    return {node["node_id"]: node for node in result.structured_content["result"]["nodes"]}


async def test_an_analog_tag_says_it_is_an_analog_tag(server):
    """The type is what tells a reading with a unit from a bare number.

    `ScratchAnalog` and the plain `Temperature` beside it are both `Variable`
    and both Double, and only one of them will answer with °C and a range. The
    class cannot express that difference; the type definition is exactly it.
    """
    impl, params = server
    async with connect(params) as session:
        nodes = await browse(session, {"depth": 4, "node_class": "Variable"})

    assert nodes[NODE["ScratchAnalog"]]["type_definition"] == "AnalogItemType", impl
    assert nodes[NODE["Temperature"]]["type_definition"] == "BaseDataVariableType", impl


async def test_an_alarm_says_it_is_an_alarm(alarm_server):
    """The case the node class cannot express at all.

    An `ExclusiveLimitAlarmType` and the folder holding it are both `Object`.
    Without the type definition, deciding whether `act_on_alarm` applies to a
    node means calling it and seeing what happens.
    """
    impl, params = alarm_server
    async with connect(params) as session:
        nodes = await browse(session, {"depth": 4})

    types = {node["type_definition"] for node in nodes.values()}
    assert "ExclusiveLimitAlarmType" in types, f"{impl}: {sorted(t for t in types if t)}"


async def test_a_folder_is_a_folder(server):
    """The default browse — depth 1 off the Objects root — is already typed."""
    impl, params = server
    async with connect(params) as session:
        nodes = await browse(session, {})

    assert nodes[NODE["IndustrialControlSystem"]]["type_definition"] == "FolderType", impl


async def test_a_node_class_with_no_type_says_null_rather_than_guessing(server):
    """A Method has no HasTypeDefinition, and that is an answer, not a failure.

    A server returning Good and no references is saying "this node has no type".
    Reporting that as a read failure, or inventing a type for it, would both be
    worse than null.
    """
    impl, params = server
    async with connect(params) as session:
        nodes = await browse(session, {"depth": 4, "node_class": "Method"})

    assert nodes, f"{impl}: the mock has methods and the browse found none"
    assert all(node["type_definition"] is None for node in nodes.values()), impl


async def test_the_types_do_not_cost_a_round_trip_each(server):
    """One batched browse for the whole result, not one per node.

    500 nodes is the default budget. If this were per-node it would be 500 extra
    round trips, and the tool would stop answering on real equipment long before
    anyone noticed it was slow. A wall-clock bound is crude, but the failure it
    guards against is two orders of magnitude, not a few percent.
    """
    impl, params = server
    async with connect(params) as session:
        started = time.monotonic()
        nodes = await browse(session, {"depth": 5, "max_nodes": 200})
        elapsed = time.monotonic() - started

    typed = [node for node in nodes.values() if node["type_definition"]]
    assert len(typed) >= 20, f"{impl}: only {len(typed)} of {len(nodes)} nodes came back typed"
    assert elapsed < 10, f"{impl}: {len(nodes)} nodes took {elapsed:.1f}s"


async def test_both_runtimes_report_the_same_types(opcua_server):
    """The whole reason the reference type and the chunk size live in the contract.

    Two clients browsing the same server must not disagree about what its nodes
    are — and the ambiguity rule (exactly one HasTypeDefinition, or null) exists
    precisely so that a nonconformant server cannot make them.
    """
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    answers = {}
    for impl in ("python", "node"):
        async with connect(_server_params(impl, opcua_server)) as session:
            nodes = await browse(session, {"depth": 4})
        answers[impl] = {node_id: node["type_definition"] for node_id, node in nodes.items()}

    assert answers["python"] == answers["node"]
