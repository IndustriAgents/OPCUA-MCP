"""What a value *means*, end to end, against a server that actually says (#110).

Everything about `nodeValues.engineering` that can be checked without a server is
checked in `tests/unit/test_value_bounds.py` and its Node twin. What needs one is
the part that matters: resolving three properties through
TranslateBrowsePathsToNodeIds and reading them back, on both runtimes, from the
same address space — because two implementations of "find the EURange" can
diverge in ways no shared table can catch.

The mock's `ScratchAnalog` (`ns=2;i=90`) is the AnalogItemType this runs against:
°C, EURange 0 to 150, InstrumentRange -50 to 250. The two ranges are deliberately
different, so a test can tell which one was enforced.

Run:
    cd tests && uv run --no-sync pytest e2e/test_engineering_units_e2e.py -v
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from mcp import StdioServerParameters
from test_mcp_e2e import NODE, NODE_BUILD, ROOT, connect, records_of, text_of

POLICY_ENV = {
    "OPCUA_PROFILE",
    "OPCUA_POLICY_FILE",
    "OPCUA_ALLOWED_TOOLS",
    "OPCUA_ALLOWED_WRITE_NODES",
    "OPCUA_ALLOWED_METHODS",
    "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS",
    "OPCUA_ALLOW_INSECURE_CONTROL",
    "OPCUA_ALLOW_OUT_OF_RANGE_WRITES",
}


def params(impl: str, url: str, **extra: str) -> StdioServerParameters:
    """An operator deployment allowed to write the one analogue node."""
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env.update(
        {
            "OPCUA_SERVER_URL": url,
            "OPCUA_PROFILE": "operator",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_ALLOWED_WRITE_NODES": f"{NODE['ScratchAnalog']},{NODE['ScratchDouble']}",
        }
    )
    env.update(extra)
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


@pytest.fixture(params=["python", "node"])
def impl(request):
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return request.param


async def test_an_analogue_node_reports_its_unit_and_both_ranges(impl, opcua_server):
    """The whole point: 51.75 with a unit is a different fact from 51.75.

    Byte-for-byte the same record on both runtimes — two implementations of
    "resolve three properties and read them" is exactly the kind of thing that
    diverges quietly.
    """
    async with connect(params(impl, opcua_server)) as session:
        result = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]})

    assert not result.is_error, text_of(result)
    [record] = records_of(result)
    assert record["engineering"] == {
        "unit": "°C",
        "unit_description": "degree Celsius",
        "eu_range": {"low": 0.0, "high": 150.0},
        "instrument_range": {"low": -50.0, "high": 250.0},
    }, f"{impl}: {record}"


async def test_a_node_that_publishes_nothing_says_so_rather_than_guessing(impl, opcua_server):
    """Most of an address space is bare values, and `engineering: null` is honest."""
    async with connect(params(impl, opcua_server)) as session:
        result = await session.call_tool(
            "read_opcua_nodes",
            {"node_ids": [NODE["ScratchDouble"], NODE["Temperature"]]},
        )

    assert not result.is_error, text_of(result)
    for record in records_of(result):
        assert record["engineering"] is None, f"{impl}: {record}"


async def test_a_mixed_batch_gets_each_node_its_own_answer(impl, opcua_server):
    """One translate and one read for the batch, and each node still gets its own.

    The batching is what makes this affordable — three round trips per node would
    make a 500-node read unusable — and this is the test that the batching did not
    smear one node's properties across another's record.
    """
    async with connect(params(impl, opcua_server)) as session:
        result = await session.call_tool(
            "read_opcua_nodes",
            {"node_ids": [NODE["ScratchDouble"], NODE["ScratchAnalog"], NODE["Pressure"]]},
        )

    assert not result.is_error, text_of(result)
    by_node = {record["node_id"]: record for record in records_of(result)}
    assert by_node[NODE["ScratchDouble"]]["engineering"] is None, impl
    assert by_node[NODE["Pressure"]]["engineering"] is None, impl
    assert by_node[NODE["ScratchAnalog"]]["engineering"]["unit"] == "°C", impl


async def test_a_second_read_is_served_from_the_cache(impl, opcua_server):
    """A node's unit does not change while a session lasts, so it is read once.

    What is observable from out here is only that the answer is stable — the
    saving is in the round trips, and the record must be identical either way.
    """
    async with connect(params(impl, opcua_server)) as session:
        first = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]})
        second = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]})

    assert records_of(first)[0]["engineering"] == records_of(second)[0]["engineering"], impl


# --- and what the range is for ---------------------------------------------------


async def test_a_write_inside_the_plants_own_range_is_allowed(impl, opcua_server):
    async with connect(params(impl, opcua_server)) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 51.75}]},
        )
        assert not result.is_error, f"{impl}: {text_of(result)}"

        readback = await session.call_tool(
            "read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]}
        )

    assert records_of(readback)[0]["value"] == 51.75, f"{impl}: {text_of(readback)}"


async def test_a_write_outside_it_is_refused_with_nothing_sent(impl, opcua_server):
    """The safety bound nobody had to type into a policy file.

    `ns=2;i=90` is fully allowlisted, so identity authorization says yes. What
    refuses this is the range the equipment itself published — which is the point
    of #110: a correctly allowlisted setpoint with a hallucinated value is still a
    physical incident.
    """
    async with connect(params(impl, opcua_server)) as session:
        before = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]})
        was = records_of(before)[0]["value"]

        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 200}]},
        )
        assert result.is_error, impl
        assert text_of(result) == (
            "200 is outside the range node ns=2;i=90 accepts (0 to 150 °C), set by the "
            "OPC UA server's own EURange. Nothing was written. Read the node to see where "
            "it is now, or widen the bound if this is deliberate."
        ), f"{impl}: {text_of(result)}"

        after = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]})

    assert records_of(after)[0]["value"] == was, f"{impl}: the refused write reached the plant"


async def test_it_is_the_eu_range_that_is_enforced_not_the_instrument_range(impl, opcua_server):
    """200 is inside what the instrument can return and outside what the process expects.

    The two ranges exist to be different: `InstrumentRange` is the edge of what a
    device can represent, and writing to it is not the same as writing something
    the process expects. This is the test that says which one the write path
    reads.
    """
    async with connect(params(impl, opcua_server)) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 200}]},
        )

    assert result.is_error, impl
    # Inside InstrumentRange (-50 to 250), outside EURange (0 to 150).
    assert "(0 to 150 °C)" in text_of(result), f"{impl}: {text_of(result)}"


async def test_one_bad_value_refuses_the_whole_batch(impl, opcua_server):
    """A batch is all-or-nothing, as it already was for a forbidden target.

    A batch that applied the acceptable half would leave the plant in a state
    nobody asked for and no record of which half landed.
    """
    async with connect(params(impl, opcua_server)) as session:
        before = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchDouble"]]})
        was = records_of(before)[0]["value"]

        result = await session.call_tool(
            "write_opcua_nodes",
            {
                "nodes": [
                    {"node_id": NODE["ScratchDouble"], "value": was + 1},
                    {"node_id": NODE["ScratchAnalog"], "value": 200},
                ]
            },
        )
        assert result.is_error, impl

        after = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchDouble"]]})

    assert records_of(after)[0]["value"] == was, f"{impl}: half the batch landed"


async def test_the_operator_can_turn_the_range_check_off(impl, opcua_server):
    """Some deployments have to write outside normal operation on purpose.

    Commissioning, forcing a value during a test, driving an actuator past its
    process range deliberately. The escape hatch is explicit and named, the way
    `OPCUA_ALLOW_INSECURE_CONTROL` is.
    """
    async with connect(
        params(impl, opcua_server, OPCUA_ALLOW_OUT_OF_RANGE_WRITES="true")
    ) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 200}]},
        )
        assert not result.is_error, f"{impl}: {text_of(result)}"

        readback = await session.call_tool(
            "read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]}
        )

    assert records_of(readback)[0]["value"] == 200, f"{impl}: {text_of(readback)}"
    # Still reported, even with enforcement off: knowing the value is outside the
    # range is exactly what makes writing it deliberate rather than accidental.
    assert records_of(readback)[0]["engineering"]["eu_range"] == {"low": 0.0, "high": 150.0}


# --- the bounds an operator writes, through a real policy file --------------------


def policy_params(impl: str, url: str, writable_nodes: list) -> StdioServerParameters:
    """An operator deployment configured from a JSON file rather than the environment.

    `OPCUA_ALLOWED_WRITE_NODES` is a comma-separated list and cannot carry a
    bound, so this is the only way to deploy one — which makes the file path
    worth one end-to-end test of its own on both runtimes.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(
            {
                "version": 1,
                "profile": "operator",
                "allow_insecure_control": True,
                "control": {"writable_nodes": writable_nodes},
            },
            handle,
        )
        path = handle.name
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env.update({"OPCUA_SERVER_URL": url, "OPCUA_POLICY_FILE": path})
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


async def test_an_operator_bound_narrows_the_plants_own_range(impl, opcua_server):
    """Both bounds apply, so a policy file can only ever narrow. 60 is inside the
    server's 0-to-150 and outside the operator's 0-to-55."""
    params_with_bound = policy_params(
        impl, opcua_server, [{"node": NODE["ScratchAnalog"], "min": 0, "max": 55}]
    )
    async with connect(params_with_bound) as session:
        allowed = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 50}]},
        )
        assert not allowed.is_error, f"{impl}: {text_of(allowed)}"

        refused = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 60}]},
        )

    assert refused.is_error, impl
    assert text_of(refused) == (
        "60 is outside the range node ns=2;i=90 accepts (0 to 55), set by the operator "
        "policy. Nothing was written. Read the node to see where it is now, or widen the "
        "bound if this is deliberate."
    ), f"{impl}: {text_of(refused)}"


async def test_max_change_is_measured_against_where_the_node_actually_is(impl, opcua_server):
    """The one bound that needs a read, checked against the live value.

    Not a bound on the value but on the *move*, which is what makes it the right
    shape for a setpoint that must be walked rather than jumped.
    """
    params_with_bound = policy_params(
        impl, opcua_server, [{"node": NODE["ScratchAnalog"], "max_change": 5}]
    )
    async with connect(params_with_bound) as session:
        # Put the node somewhere known first — a move is relative to where it is.
        start = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 50}]},
        )
        assert not start.is_error, f"{impl}: {text_of(start)}"

        near = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 54}]},
        )
        assert not near.is_error, f"{impl}: {text_of(near)}"

        far = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": NODE["ScratchAnalog"], "value": 100}]},
        )
        assert far.is_error, impl
        assert text_of(far) == (
            "Moving node ns=2;i=90 from 54 to 100 is a change of 46, and the operator "
            "policy allows at most 5 in one write. Nothing was written. Make the move in "
            "steps if it is deliberate."
        ), f"{impl}: {text_of(far)}"

        after = await session.call_tool("read_opcua_nodes", {"node_ids": [NODE["ScratchAnalog"]]})

    assert records_of(after)[0]["value"] == 54, f"{impl}: the refused move reached the plant"
