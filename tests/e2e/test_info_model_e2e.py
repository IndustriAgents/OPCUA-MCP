"""End-to-end checks driven by the OPC UA information model, on both runtimes.

The unit suites pin every rule through the shared tables in ``tests/fixtures/``
(``write-plan.json``, ``method-plan.json``, ``write-access.json``,
``policy-check.json``, ``preconditions.json``, ``alarm-scope.json``). What they
cannot show is that the attributes those rules read actually arrive from a
server: that a read-only node is *seen* as read-only, that an enumeration's
labels are found on its DataType, that a browse path resolves, that a deny
list hides a subtree. That needs the mock, whose ``_create_info_model_probes``
publishes a node for each case (ids from ``ns=2;i=102`` up).

Every expected sentence is built from ``contract/tools.json`` here rather than
copied, so a rewording changes one place.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager

import pytest
from conftest import ALARM_TEMPERATURE_NODE_ID, ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_e2e import NODE_BUILD, connect, records_of, text_of
from test_policy_e2e import MOCK_NAMESPACE_URI, POLICY_ENV, audit_records

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))

# Mock nodes this file uses. The originals keep their places in
# `test_mcp_e2e.NODE`; the ones from 102 up are the information-model probes.
TEMPERATURE = "ns=2;i=3"  # read-only (AccessLevel CurrentRead | HistoryRead)
INDUSTRIAL_CONTROL_SYSTEM = "ns=2;i=1"
METHODS = "ns=2;i=27"
START_PRODUCTION = "ns=2;i=28"  # one Double argument
STOP_PRODUCTION = "ns=2;i=31"
SCRATCH_DOUBLE = "ns=2;i=41"
SCRATCH_BOOLEAN = "ns=2;i=42"
SCRATCH_ANALOG = "ns=2;i=90"  # EURange 0..150 °C, InstrumentRange -50..250
MACHINE_STATE = "ns=2;i=102"  # DataType ServerState: an enumeration
VALVE_MODE = "ns=2;i=103"  # MultiStateDiscreteType: Closed, Open, Auto
DOOR_LOCK = "ns=2;i=105"  # TwoStateDiscreteType: Locked / Unlocked
SCRATCH_ARRAY = "ns=2;i=108"  # Double[3], ValueRank 1, ArrayDimensions [3]
DISABLED_METHOD = "ns=2;i=109"  # Executable false
INTERLOCK_PERMIT = "ns=2;i=110"
INTERLOCKED_SETPOINT = "ns=2;i=111"
LINE_SPEED = "ns=2;i=113"  # AnalogItemType under Line1
LINE_TENSION = "ns=2;i=115"  # AnalogItemType under Line1
LINE_LABEL = "ns=2;i=117"  # plain String under Line1
RECIPES = "ns=2;i=118"
SECRET_RECIPE = "ns=2;i=119"
WRITE_ONLY = "ns=2;i=120"  # AccessLevel CurrentWrite only, value reads BadNotReadable

LINE1_PATH = "/Objects/IndustrialControlSystem/Line1"
RECIPES_PATH = "/Objects/IndustrialControlSystem/Recipes"
SCRATCH_DOUBLE_PATH = "/Objects/IndustrialControlSystem/Scratch/ScratchDouble"
ANALOG_ITEM_TYPE = "i=2368"

SERVER_STATES = [
    "Running",
    "Failed",
    "NoConfiguration",
    "Suspended",
    "Shutdown",
    "Test",
    "CommunicationFault",
    "Unknown",
]


def sentence(section: str, key: str, **values) -> str:
    """One contract sentence with its placeholders filled, as both runtimes fill them."""
    text = (
        CONTRACT[section]["messages"][key] if section == "policyCheck" else CONTRACT[section][key]
    )
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def params(impl: str, url: str, **env: str) -> StdioServerParameters:
    """A server with no inherited policy, and exactly the settings given."""
    environment = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    environment.pop("OPCUA_ALLOW_OUT_OF_RANGE_WRITES", None)
    environment.update({"OPCUA_SERVER_URL": url, **env})
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=environment,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=environment)


def full(impl: str, url: str, **env: str) -> StdioServerParameters:
    """The lab-only full profile the shared suite uses for control."""
    return params(impl, url, OPCUA_PROFILE="full", OPCUA_ALLOW_INSECURE_CONTROL="true", **env)


def with_policy(impl: str, url: str, policy: dict, directory: str, **env: str):
    """A server reading `policy` from a file — the only place most of it can be said."""
    path = os.path.join(directory, f"policy-{impl}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, **policy}, handle)
    return params(impl, url, OPCUA_POLICY_FILE=path, **env)


@asynccontextmanager
async def connect_capturing_stderr(server: StdioServerParameters):
    with tempfile.TemporaryFile("w+", errors="replace") as errlog:
        async with (
            stdio_client(server, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session, errlog
        errlog.seek(0)


async def write(session, *nodes: dict):
    return await session.call_tool("write_opcua_nodes", {"nodes": list(nodes)})


async def read_value(session, node_id: str):
    result = await session.call_tool("read_opcua_nodes", {"node_ids": [node_id]})
    assert not result.is_error, text_of(result)
    return records_of(result)[0]["value"]


async def server_status(session) -> dict:
    result = await session.call_tool("get_server_status", {})
    assert not result.is_error, text_of(result)
    return result.structured_content["result"]


@pytest.fixture(params=["python", "node"])
def impl(request):
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return request.param


# --- what a node says about being written -------------------------------------------


async def test_a_read_only_node_is_skipped_and_the_rest_of_the_batch_is_written(impl, opcua_server):
    """AccessLevel without CurrentWrite: that node's record says so, nothing is sent to
    it, and the other write in the batch still lands — the same per-node shape as a
    value that will not convert."""
    async with connect(full(impl, opcua_server)) as session:
        result = await write(
            session,
            {"node_id": TEMPERATURE, "value": 20},
            {"node_id": SCRATCH_DOUBLE, "value": 61.25},
        )
        stored = await read_value(session, SCRATCH_DOUBLE)

    assert not result.is_error, f"{impl}: {text_of(result)}"
    skipped, written = records_of(result)
    assert skipped == {
        "node_id": TEMPERATURE,
        "status": "BadNotWritable",
        "error": sentence("writeSkips", "notWritable", node_id=TEMPERATURE),
    }, impl
    assert written["status"] == "Good", f"{impl}: {written}"
    assert stored == 61.25, impl


async def test_a_node_that_is_not_a_variable_has_nothing_to_write(impl, opcua_server):
    async with connect(full(impl, opcua_server)) as session:
        result = await write(session, {"node_id": METHODS, "value": 1})

    [record] = records_of(result)
    assert record == {
        "node_id": METHODS,
        "status": "BadNodeClassInvalid",
        "error": sentence("writeSkips", "notVariable", node_id=METHODS, node_class="Object"),
    }, impl


async def test_a_write_only_node_is_typed_from_its_data_type_attribute(impl, opcua_server):
    """Its value reads BadNotReadable, which is what used to stop a write that did not
    name data_type: the type was read off the current value. It now comes from the
    DataType attribute, which a write-only node still publishes."""
    async with connect(full(impl, opcua_server)) as session:
        result = await write(session, {"node_id": WRITE_ONLY, "value": "12.5"})

    [record] = records_of(result)
    assert record["status"] == "Good", f"{impl}: {record}"
    assert record["error"] is None, impl


async def test_an_explicit_data_type_must_match_the_node(impl, opcua_server):
    async with connect(full(impl, opcua_server)) as session:
        result = await write(session, {"node_id": SCRATCH_DOUBLE, "value": 1, "data_type": "Float"})

    [record] = records_of(result)
    assert record == {
        "node_id": SCRATCH_DOUBLE,
        "status": "BadTypeMismatch",
        "error": sentence(
            "writeSkips",
            "typeMismatch",
            node_id=SCRATCH_DOUBLE,
            node_type="Double",
            data_type="Float",
        ),
    }, impl


async def test_a_node_says_whether_it_holds_one_value_or_an_array(impl, opcua_server):
    async with connect(full(impl, opcua_server)) as session:
        list_to_scalar = await write(session, {"node_id": SCRATCH_DOUBLE, "value": [1, 2]})
        scalar_to_array = await write(session, {"node_id": SCRATCH_ARRAY, "value": 1})
        too_long = await write(session, {"node_id": SCRATCH_ARRAY, "value": [1, 2, 3, 4]})
        fits = await write(session, {"node_id": SCRATCH_ARRAY, "value": [1.5, 2.5, 3.5]})
        stored = await read_value(session, SCRATCH_ARRAY)

    assert records_of(list_to_scalar)[0]["error"] == sentence(
        "writeSkips", "needsScalar", node_id=SCRATCH_DOUBLE
    ), impl
    assert records_of(scalar_to_array)[0]["error"] == sentence(
        "writeSkips", "needsArray", node_id=SCRATCH_ARRAY, value_rank=1
    ), impl
    assert records_of(too_long)[0] == {
        "node_id": SCRATCH_ARRAY,
        "status": "BadOutOfRange",
        "error": sentence("writeSkips", "arrayTooLong", node_id=SCRATCH_ARRAY, limit=3, count=4),
    }, impl
    assert records_of(fits)[0]["status"] == "Good", f"{impl}: {text_of(fits)}"
    assert stored == [1.5, 2.5, 3.5], impl


async def test_an_enumeration_takes_a_label_found_on_its_data_type(impl, opcua_server):
    """MachineState's DataType is ServerState, whose EnumStrings live on the DataType
    node rather than the variable — the usual place, and the harder one to find."""
    async with connect(full(impl, opcua_server)) as session:
        by_label = await write(session, {"node_id": MACHINE_STATE, "value": "Suspended"})
        stored = await read_value(session, MACHINE_STATE)
        wrong_case = await write(session, {"node_id": MACHINE_STATE, "value": "suspended"})
        reset = await write(session, {"node_id": MACHINE_STATE, "value": 0})

    assert records_of(by_label)[0]["status"] == "Good", f"{impl}: {text_of(by_label)}"
    assert stored == 3, impl
    allowed = ", ".join(f"{index} = {label}" for index, label in enumerate(SERVER_STATES))
    assert wrong_case.is_error is True, impl
    assert text_of(wrong_case) == sentence(
        "errors", "valueNotAState", value='"suspended"', node_id=MACHINE_STATE, allowed=allowed
    ), impl
    assert records_of(reset)[0]["status"] == "Good", impl


async def test_a_multistate_node_takes_a_label_found_on_the_variable(impl, opcua_server):
    async with connect(full(impl, opcua_server)) as session:
        result = await write(session, {"node_id": VALVE_MODE, "value": "Auto"})
        stored = await read_value(session, VALVE_MODE)

    assert records_of(result)[0]["status"] == "Good", f"{impl}: {text_of(result)}"
    assert stored == 2, impl


async def test_a_two_state_node_takes_its_labels(impl, opcua_server):
    async with connect(full(impl, opcua_server)) as session:
        await write(session, {"node_id": DOOR_LOCK, "value": "Locked"})
        locked = await read_value(session, DOOR_LOCK)
        await write(session, {"node_id": DOOR_LOCK, "value": "Unlocked"})
        unlocked = await read_value(session, DOOR_LOCK)

    assert locked is True, impl
    assert unlocked is False, impl


async def test_the_instrument_range_holds_even_when_the_eu_range_is_lifted(impl, opcua_server):
    async with connect(full(impl, opcua_server, OPCUA_ALLOW_OUT_OF_RANGE_WRITES="true")) as session:
        outside_normal = await write(session, {"node_id": SCRATCH_ANALOG, "value": 200})
        outside_device = await write(session, {"node_id": SCRATCH_ANALOG, "value": 300})
        await write(session, {"node_id": SCRATCH_ANALOG, "value": 50})

    assert records_of(outside_normal)[0]["status"] == "Good", f"{impl}: {text_of(outside_normal)}"
    assert outside_device.is_error is True, impl
    assert text_of(outside_device) == sentence(
        "errors",
        "valueOutOfRange",
        value=300,
        node_id=SCRATCH_ANALOG,
        low=-50,
        high=250,
        unit=" °C",
        source="the instrument's own InstrumentRange",
    ), impl


# --- methods -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            {"object_node_id": METHODS, "method_node_id": DISABLED_METHOD},
            sentence("errors", "methodNotExecutable", method_node_id=DISABLED_METHOD),
        ),
        (
            {
                "object_node_id": INDUSTRIAL_CONTROL_SYSTEM,
                "method_node_id": START_PRODUCTION,
                "arguments": [60],
            },
            sentence(
                "errors",
                "methodNotOnObject",
                method_node_id=START_PRODUCTION,
                object_node_id=INDUSTRIAL_CONTROL_SYSTEM,
            ),
        ),
        (
            {"object_node_id": METHODS, "method_node_id": START_PRODUCTION},
            sentence(
                "errors",
                "methodArgumentCount",
                method_node_id=START_PRODUCTION,
                expected=1,
                count=0,
            ),
        ),
        (
            {"object_node_id": METHODS, "method_node_id": SCRATCH_DOUBLE},
            sentence(
                "errors", "methodNotAMethod", method_node_id=SCRATCH_DOUBLE, node_class="Variable"
            ),
        ),
    ],
    ids=["not-executable", "not-on-object", "argument-count", "not-a-method"],
)
async def test_a_method_call_the_address_space_rules_out_is_not_sent(
    impl, opcua_server, call, expected
):
    async with connect(full(impl, opcua_server)) as session:
        result = await session.call_tool("call_opcua_method", call)

    assert result.is_error is True, impl
    assert text_of(result) == expected, impl


# --- write_access on reads -------------------------------------------------------


async def test_a_read_can_say_how_each_node_may_be_written(impl, opcua_server):
    nodes = [SCRATCH_ANALOG, TEMPERATURE, MACHINE_STATE, DOOR_LOCK, SCRATCH_ARRAY, METHODS]
    async with connect(full(impl, opcua_server)) as session:
        plain = await session.call_tool("read_opcua_nodes", {"node_ids": nodes[:1]})
        result = await session.call_tool(
            "read_opcua_nodes", {"node_ids": nodes, "include_write_access": True}
        )

    assert records_of(plain)[0]["write_access"] is None, impl
    assert not result.is_error, f"{impl}: {text_of(result)}"
    access = {record["node_id"]: record["write_access"] for record in records_of(result)}

    assert access[SCRATCH_ANALOG] == {
        "allowed": True,
        "reason": None,
        "data_type": "Double",
        "array": False,
        "states": None,
        "min": 0,
        "max": 150,
        "allowed_values": None,
        "max_change": None,
    }, impl
    assert access[TEMPERATURE]["allowed"] is False, impl
    assert access[TEMPERATURE]["reason"] == CONTRACT["writeAccessReasons"]["readOnly"], impl
    assert access[MACHINE_STATE]["data_type"] == "Int32", impl
    assert access[MACHINE_STATE]["states"] == [
        {"value": index, "label": label} for index, label in enumerate(SERVER_STATES)
    ], impl
    assert access[DOOR_LOCK]["states"] == [
        {"value": True, "label": "Locked"},
        {"value": False, "label": "Unlocked"},
    ], impl
    assert access[SCRATCH_ARRAY]["array"] is True, impl
    assert access[METHODS]["reason"] == sentence(
        "writeAccessReasons", "notVariable", node_class="Object"
    ), impl


async def test_write_access_carries_the_operator_policy(impl, opcua_server, tmp_path):
    policy = {
        "profile": "operator",
        "allow_insecure_control": True,
        "control": {
            "writable_nodes": [{"node": SCRATCH_ANALOG, "min": 10, "max": 80, "max_change": 5}]
        },
    }
    async with connect(with_policy(impl, opcua_server, policy, str(tmp_path))) as session:
        result = await session.call_tool(
            "read_opcua_nodes",
            {"node_ids": [SCRATCH_ANALOG, SCRATCH_DOUBLE], "include_write_access": True},
        )

    analog, scratch = (record["write_access"] for record in records_of(result))
    assert (analog["allowed"], analog["min"], analog["max"], analog["max_change"]) == (
        True,
        10,
        80,
        5,
    ), f"{impl}: {analog}"
    assert scratch["allowed"] is False, impl
    assert scratch["reason"] == sentence("errors", "nodeNotWritable", node_id=SCRATCH_DOUBLE), impl


# --- the policy, resolved against the server -------------------------------------


async def test_a_browse_path_allowlist_entry_authorizes_the_node_it_resolves_to(impl, opcua_server):
    server = params(
        impl,
        opcua_server,
        OPCUA_PROFILE="operator",
        OPCUA_ALLOW_INSECURE_CONTROL="true",
        OPCUA_ALLOWED_WRITE_NODES=SCRATCH_DOUBLE_PATH,
    )
    async with connect(server) as session:
        allowed = await write(session, {"node_id": SCRATCH_DOUBLE, "value": 3.5})
        denied = await write(session, {"node_id": SCRATCH_BOOLEAN, "value": True})

    assert not allowed.is_error, f"{impl}: {text_of(allowed)}"
    assert records_of(allowed)[0]["status"] == "Good", impl
    assert denied.is_error is True, impl
    assert text_of(denied) == sentence("errors", "nodeNotWritable", node_id=SCRATCH_BOOLEAN), impl


async def test_the_policy_check_reports_what_the_server_says_cannot_work(
    impl, opcua_server, tmp_path
):
    missing = "nsu=urn:not-published;i=1"
    policy = {
        "profile": "operator",
        "allow_insecure_control": True,
        "control": {
            "writable_nodes": [TEMPERATURE, METHODS, missing, SCRATCH_DOUBLE_PATH],
            "callable_methods": [
                {"object_id": METHODS, "method_id": DISABLED_METHOD},
                {"object_id": INDUSTRIAL_CONTROL_SYSTEM, "method_id": START_PRODUCTION},
            ],
        },
    }
    server = with_policy(impl, opcua_server, policy, str(tmp_path))
    async with connect_capturing_stderr(server) as (session, errlog):
        status = await server_status(session)
        errlog.seek(0)
        stderr = errlog.read()

    check = status["policy_check"]
    expected = [
        {
            "entry": TEMPERATURE,
            "problem": sentence("policyCheck", "readOnly", entry=TEMPERATURE, node_id=TEMPERATURE),
        },
        {
            "entry": METHODS,
            "problem": sentence(
                "policyCheck", "notVariable", entry=METHODS, node_id=METHODS, node_class="Object"
            ),
        },
        {"entry": missing, "problem": sentence("policyCheck", "unresolved", entry=missing)},
        {
            "entry": f"{METHODS}|{DISABLED_METHOD}",
            "problem": sentence(
                "policyCheck", "methodNotExecutable", entry=f"{METHODS}|{DISABLED_METHOD}"
            ),
        },
        {
            "entry": f"{INDUSTRIAL_CONTROL_SYSTEM}|{START_PRODUCTION}",
            "problem": sentence(
                "policyCheck",
                "methodNotOnObject",
                entry=f"{INDUSTRIAL_CONTROL_SYSTEM}|{START_PRODUCTION}",
                method_node_id=START_PRODUCTION,
                object_node_id=INDUSTRIAL_CONTROL_SYSTEM,
            ),
        },
    ]
    assert check["findings"] == expected, f"{impl}: {check['findings']}"
    assert check["writable_nodes"] == sorted([TEMPERATURE, METHODS, SCRATCH_DOUBLE]), impl
    assert check["callable_methods"] == sorted(
        [f"{METHODS}|{DISABLED_METHOD}", f"{INDUSTRIAL_CONTROL_SYSTEM}|{START_PRODUCTION}"]
    ), impl
    assert isinstance(check["generation"], int), impl
    for finding in expected:
        assert f"WARNING: policy check: {finding['problem']}" in stderr, f"{impl}: {stderr}"


async def test_a_writable_subtree_allows_the_typed_nodes_under_it(impl, opcua_server, tmp_path):
    policy = {
        "profile": "operator",
        "allow_insecure_control": True,
        "control": {
            "writable_subtrees": [
                {"root": LINE1_PATH, "type_definition": ANALOG_ITEM_TYPE, "max": 40}
            ]
        },
    }
    async with connect(with_policy(impl, opcua_server, policy, str(tmp_path))) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        allowed = await write(session, {"node_id": LINE_SPEED, "value": 30})
        too_fast = await write(session, {"node_id": LINE_SPEED, "value": 45})
        untyped = await write(session, {"node_id": LINE_LABEL, "value": "L2"})
        status = await server_status(session)

    assert "write_opcua_nodes" in tools, impl
    assert records_of(allowed)[0]["status"] == "Good", f"{impl}: {text_of(allowed)}"
    assert too_fast.is_error is True, impl
    assert "set by the operator policy" in text_of(too_fast), f"{impl}: {text_of(too_fast)}"
    assert untyped.is_error is True, impl
    assert text_of(untyped) == sentence("errors", "nodeNotWritable", node_id=LINE_LABEL), impl
    assert status["policy_check"]["writable_nodes"] == [LINE_SPEED, LINE_TENSION], impl


async def test_deny_read_hides_a_subtree_from_every_read(impl, opcua_server, tmp_path):
    policy = {"deny_read": [RECIPES_PATH]}
    async with connect(with_policy(impl, opcua_server, policy, str(tmp_path))) as session:
        # The first call is what connects; the policy is resolved on the session.
        status = await server_status(session)
        secret = await session.call_tool("read_opcua_nodes", {"node_ids": [SECRET_RECIPE]})
        folder = await session.call_tool("browse_opcua_nodes", {"node_id": RECIPES})
        subscribed = await session.call_tool("subscribe_opcua_nodes", {"node_ids": [SECRET_RECIPE]})
        browsed = await session.call_tool(
            "browse_opcua_nodes", {"node_id": INDUSTRIAL_CONTROL_SYSTEM, "depth": 2}
        )
        ordinary = await session.call_tool("read_opcua_nodes", {"node_ids": [SCRATCH_DOUBLE]})

    check = status["policy_check"]
    assert check["read_denied"] == 2, f"{impl}: {check}"
    assert check["read_policy_complete"] is True, impl
    assert check["findings"] == [], impl
    for refused, node_id in (
        (secret, SECRET_RECIPE),
        (folder, RECIPES),
        (subscribed, SECRET_RECIPE),
    ):
        assert refused.is_error is True, impl
        assert text_of(refused) == sentence("errors", "readDenied", node_id=node_id), impl
    listed = {node["node_id"] for node in browsed.structured_content["result"]["nodes"]}
    assert RECIPES not in listed and SECRET_RECIPE not in listed, f"{impl}: {sorted(listed)}"
    assert SCRATCH_DOUBLE in listed, impl
    assert not ordinary.is_error, impl


async def test_a_precondition_holds_a_write_until_its_interlock_is_set(
    impl, opcua_server, tmp_path
):
    policy = {
        "profile": "operator",
        "allow_insecure_control": True,
        "control": {
            "writable_nodes": [INTERLOCK_PERMIT, INTERLOCKED_SETPOINT],
            "preconditions": [
                {
                    "targets": [INTERLOCKED_SETPOINT],
                    "require": [{"node": INTERLOCK_PERMIT, "equals": True}],
                }
            ],
        },
    }
    async with connect(with_policy(impl, opcua_server, policy, str(tmp_path))) as session:
        await write(session, {"node_id": INTERLOCK_PERMIT, "value": False})
        held = await write(session, {"node_id": INTERLOCKED_SETPOINT, "value": 5})
        await write(session, {"node_id": INTERLOCK_PERMIT, "value": True})
        released = await write(session, {"node_id": INTERLOCKED_SETPOINT, "value": 5})
        await write(session, {"node_id": INTERLOCK_PERMIT, "value": False})

    assert held.is_error is True, impl
    assert text_of(held) == sentence(
        "errors",
        "preconditionNotMet",
        target=INTERLOCKED_SETPOINT,
        requirement=f"{INTERLOCK_PERMIT} equals true",
        node_id=INTERLOCK_PERMIT,
        current="false",
    ), impl
    assert records_of(released)[0]["status"] == "Good", f"{impl}: {text_of(released)}"


async def test_a_precondition_holds_a_method_call(impl, opcua_server, tmp_path):
    policy = {
        "profile": "operator",
        "allow_insecure_control": True,
        "control": {
            "writable_nodes": [INTERLOCK_PERMIT],
            "callable_methods": [{"object_id": METHODS, "method_id": STOP_PRODUCTION}],
            "preconditions": [
                {
                    "methods": [{"object_id": METHODS, "method_id": STOP_PRODUCTION}],
                    "require": [{"node": INTERLOCK_PERMIT, "equals": True}],
                }
            ],
        },
    }
    async with connect(with_policy(impl, opcua_server, policy, str(tmp_path))) as session:
        await write(session, {"node_id": INTERLOCK_PERMIT, "value": False})
        held = await session.call_tool(
            "call_opcua_method", {"object_node_id": METHODS, "method_node_id": STOP_PRODUCTION}
        )

    assert held.is_error is True, impl
    assert text_of(held) == sentence(
        "errors",
        "preconditionNotMet",
        target=f"{METHODS}|{STOP_PRODUCTION}",
        requirement=f"{INTERLOCK_PERMIT} equals true",
        node_id=INTERLOCK_PERMIT,
        current="false",
    ), impl


async def test_the_audit_trail_names_targets_by_namespace_uri_too(impl, opcua_server):
    server = params(
        impl,
        opcua_server,
        OPCUA_PROFILE="operator",
        OPCUA_ALLOW_INSECURE_CONTROL="true",
        OPCUA_ALLOWED_WRITE_NODES=SCRATCH_DOUBLE,
    )
    async with connect_capturing_stderr(server) as (session, errlog):
        await write(session, {"node_id": SCRATCH_DOUBLE, "value": 4.5})
        records = audit_records(errlog)

    allowed = next(record for record in records if record["decision"] == "allowed")
    assert allowed["node_ids"] == [SCRATCH_DOUBLE], impl
    assert allowed["node_uris"] == [f"nsu={MOCK_NAMESPACE_URI};i=41"], f"{impl}: {allowed}"


# --- alarm scope ------------------------------------------------------------------


async def _active_alarm(session) -> dict:
    """Re-arm the alarm mock's one alarm, and return its record."""
    for value in ("20", "100"):
        await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": ALARM_TEMPERATURE_NODE_ID, "value": value}]}
        )
        await asyncio.sleep(1)
    listed = await session.call_tool("list_active_alarms", {})
    assert not listed.is_error, text_of(listed)
    return next(a for a in records_of(listed) if a["condition_name"] == "HighTemperatureAlarm")


def alarm_operator(impl: str, url: str, directory: str, control: dict) -> StdioServerParameters:
    policy = {
        "profile": "operator",
        "allow_insecure_control": True,
        "control": {
            "writable_nodes": [ALARM_TEMPERATURE_NODE_ID],
            "acknowledge_alarms": True,
            **control,
        },
    }
    return with_policy(impl, url, policy, directory)


async def test_an_alarm_outside_the_policys_sources_is_not_acknowledged(
    impl, alarm_opcua_server, tmp_path
):
    elsewhere = "ns=1;i=999999"
    server = alarm_operator(impl, alarm_opcua_server, str(tmp_path), {"alarm_sources": [elsewhere]})
    async with connect(server) as session:
        alarm = await _active_alarm(session)
        refused = await session.call_tool(
            "acknowledge_alarm", {"condition_id": alarm["condition_id"], "comment": "e2e"}
        )

    assert refused.is_error is True, impl
    assert text_of(refused) == sentence(
        "errors",
        "alarmSourceNotAllowed",
        condition_id=alarm["condition_id"],
        source_node_id=ALARM_TEMPERATURE_NODE_ID,
    ), impl


async def test_an_alarm_from_a_listed_source_is_acknowledged(impl, alarm_opcua_server, tmp_path):
    server = alarm_operator(
        impl, alarm_opcua_server, str(tmp_path), {"alarm_sources": [ALARM_TEMPERATURE_NODE_ID]}
    )
    async with connect(server) as session:
        alarm = await _active_alarm(session)
        acknowledged = await session.call_tool(
            "acknowledge_alarm", {"condition_id": alarm["condition_id"], "comment": "e2e"}
        )

    assert not acknowledged.is_error, f"{impl}: {text_of(acknowledged)}"


async def test_an_alarm_above_the_policys_severity_is_left_to_a_person(
    impl, alarm_opcua_server, tmp_path
):
    async with connect(alarm_operator(impl, alarm_opcua_server, str(tmp_path), {})) as session:
        severity = (await _active_alarm(session))["severity"]
    assert isinstance(severity, int) and severity > 1, (
        f"{impl}: the mock raised severity {severity}"
    )

    server = alarm_operator(
        impl, alarm_opcua_server, str(tmp_path), {"alarm_max_severity": severity - 1}
    )
    async with connect(server) as session:
        alarm = await _active_alarm(session)
        refused = await session.call_tool(
            "acknowledge_alarm", {"condition_id": alarm["condition_id"], "comment": "e2e"}
        )

    assert refused.is_error is True, impl
    assert text_of(refused) == sentence(
        "errors",
        "alarmTooSevere",
        condition_id=alarm["condition_id"],
        severity=alarm["severity"],
        limit=severity - 1,
    ), impl
