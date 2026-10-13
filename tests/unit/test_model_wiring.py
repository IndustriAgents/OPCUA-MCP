"""The use cases with an information model: what each gathers, and in which order.

The rules themselves are pinned by the shared tables (write-plan, method-plan,
write-access, preconditions, alarm-scope); the orchestration without a model by
the *-port.json tables. This is the seam between them: that a use case given an
``application.model.Model`` reads the facts it needs, applies the rule before
anything is sent, and keeps a refusal from reaching the native service.
"""

from __future__ import annotations

import json

import pytest
from opcua_mcp_server.application.alarms import act_on_alarm
from opcua_mcp_server.application.browse import browse_nodes
from opcua_mcp_server.application.history import read_history
from opcua_mcp_server.application.methods import call_method
from opcua_mcp_server.application.model import Model
from opcua_mcp_server.application.read import read_nodes
from opcua_mcp_server.application.write import write_nodes
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal, message
from opcua_mcp_server.node_facts import empty_facts
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config

SETPOINT = "ns=2;i=41"
TEMPERATURE = "ns=2;i=3"
PERMIT = "ns=2;i=110"
GUARDED = "ns=2;i=111"
OBJECT = "ns=2;i=27"
METHOD = "ns=2;i=31"
SKIPS = CONTRACT["writeSkips"]


def facts(**fields) -> dict:
    """A Good Variable's facts: a writable scalar Double unless told otherwise."""
    record = empty_facts("Good")
    record.update(
        node_class="Variable",
        data_type="Double",
        data_type_id="ns=0;i=11",
        value_rank=-1,
        access_level=7,
        user_access_level=7,
    )
    record.update(fields)
    return record


def full() -> ToolPolicy:
    return ToolPolicy(
        parse_policy_config({"OPCUA_PROFILE": "full", "OPCUA_ALLOW_INSECURE_CONTROL": "true"})
    )


def operator(tmp_path, control: dict) -> ToolPolicy:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "control": control}), encoding="utf-8")
    return ToolPolicy(
        parse_policy_config(
            {
                "OPCUA_PROFILE": "operator",
                "OPCUA_ALLOW_INSECURE_CONTROL": "true",
                "OPCUA_POLICY_FILE": str(path),
            }
        )
    )


class FakeModelPort:
    def __init__(self, known=None, on_object=None, readings=None, condition=None):
        self.known = known or {}
        self.answer = on_object
        self.values = readings or {}
        self.found = condition
        self.calls = []

    async def facts(self, node_ids):
        self.calls.append(["facts", node_ids])
        return {node_id: self.known.get(node_id) for node_id in node_ids}

    async def on_object(self, object_node_id, method_node_id):
        self.calls.append(["on_object", object_node_id, method_node_id])
        return self.answer

    async def readings(self, node_ids):
        self.calls.append(["readings", node_ids])
        return [self.values[node_id] for node_id in node_ids]

    async def condition(self, condition_id):
        self.calls.append(["condition", condition_id])
        return self.found


# --- writes -----------------------------------------------------------------------


class FakeWritePort:
    def __init__(self, known=None, engineering=None):
        self.calls = []
        self.known = known or {}
        self.info = engineering or {}
        self.prepared = []

    async def facts(self, node_ids):
        self.calls.append(["facts", node_ids])
        return {node_id: self.known.get(node_id) for node_id in node_ids}

    async def current(self, nodes, indices, chunk):
        self.calls.append(["current", indices])
        return {}

    async def engineering(self, node_ids):
        self.calls.append(["engineering", node_ids])
        return {node_id: self.info.get(node_id) for node_id in node_ids}

    async def prepare(self, node, index):
        self.calls.append(["prepare", index])
        self.prepared.append(node)
        return None

    async def send(self):
        self.calls.append(["send"])
        return ["Good"] * len(self.prepared)


LIMITS = {"write": 100, "read": 100}


async def test_a_planned_write_skips_a_read_only_node_and_types_the_rest():
    port = FakeWritePort({TEMPERATURE: facts(access_level=5), SETPOINT: facts()})
    nodes = [{"node_id": TEMPERATURE, "value": 20}, {"node_id": SETPOINT, "value": 61.25}]

    results = await write_nodes(port, nodes, LIMITS, {}, False)

    assert results == [
        {
            "node_id": TEMPERATURE,
            "status": "BadNotWritable",
            "error": SKIPS["notWritable"].format(node_id=TEMPERATURE),
        },
        {"node_id": SETPOINT, "status": "Good", "error": None},
    ]
    # Typed from the DataType attribute, so no current value is read, and the
    # skipped node is never prepared.
    assert port.prepared == [
        {"node_id": SETPOINT, "value": 61.25, "data_type": "Double", "array": False}
    ]
    assert [call[0] for call in port.calls] == ["facts", "engineering", "prepare", "send"]


async def test_a_planned_write_reads_the_current_value_only_where_the_type_is_unknown():
    port = FakeWritePort({SETPOINT: None})

    await write_nodes(port, [{"node_id": SETPOINT, "value": 1}], LIMITS, {}, False)

    assert ["current", [0]] in port.calls


@pytest.mark.parametrize("known", [facts(), None], ids=["facts-read", "facts-unknown"])
async def test_the_instrument_range_refuses_the_batch_even_when_the_eu_range_is_lifted(known):
    engineering = {
        SETPOINT: {
            "unit": "°C",
            "unit_description": None,
            "eu_range": {"low": 0, "high": 150},
            "instrument_range": {"low": -50, "high": 250},
        }
    }
    port = FakeWritePort({SETPOINT: known}, engineering)

    with pytest.raises(ApplicationRefusal, match="InstrumentRange"):
        await write_nodes(port, [{"node_id": SETPOINT, "value": 300}], LIMITS, {}, True)
    assert ["send"] not in port.calls


async def test_an_interlock_that_does_not_hold_refuses_the_write(tmp_path):
    policy = operator(
        tmp_path,
        {
            "writable_nodes": [PERMIT, GUARDED],
            "preconditions": [
                {"targets": [GUARDED], "require": [{"node": PERMIT, "equals": True}]}
            ],
        },
    )
    model_port = FakeModelPort(readings={PERMIT: {"status": "Good", "value": False}})
    port = FakeWritePort({GUARDED: facts()})

    with pytest.raises(ApplicationRefusal) as refused:
        await write_nodes(
            port, [{"node_id": GUARDED, "value": 5}], LIMITS, {}, False, Model(model_port, policy)
        )

    assert str(refused.value) == message(
        "preconditionNotMet",
        target=GUARDED,
        requirement=f"{PERMIT} equals true",
        node_id=PERMIT,
        current="false",
    )
    assert ["readings", [PERMIT]] in model_port.calls
    assert not any(call[0] in {"prepare", "send"} for call in port.calls)


async def test_an_interlock_whose_target_does_not_resolve_stops_every_write(tmp_path):
    missing = "/Objects/Nowhere"
    policy = operator(
        tmp_path,
        {
            "writable_nodes": [SETPOINT],
            "preconditions": [{"targets": [missing], "require": [{"node": PERMIT, "equals": 1}]}],
        },
    )
    model = Model(FakeModelPort(), policy)
    port = FakeWritePort({SETPOINT: facts()})

    with pytest.raises(ApplicationRefusal) as refused:
        await write_nodes(port, [{"node_id": SETPOINT, "value": 1}], LIMITS, {}, False, model)

    assert str(refused.value) == message("preconditionUnresolved", entry=missing)


# --- method calls -----------------------------------------------------------------


class FakeMethodPort:
    def __init__(self, declared):
        self.declared = declared
        self.calls = []

    async def input_types(self, method_node_id):
        self.calls.append("metadata")
        return self.declared

    async def call(self, object_node_id, method_node_id, arguments):
        self.calls.append("call")
        return {"status": "Good", "outputs": []}


def method_facts(**fields) -> dict:
    record = empty_facts("Good")
    record.update(node_class="Method", executable=True, user_executable=True)
    record.update(fields)
    return record


def object_facts() -> dict:
    record = empty_facts("Good")
    record.update(node_class="Object")
    return record


async def test_a_method_that_is_not_executable_is_never_called():
    port = FakeMethodPort([])
    model_port = FakeModelPort(
        {OBJECT: object_facts(), METHOD: method_facts(executable=False)}, on_object=True
    )

    with pytest.raises(ApplicationRefusal) as refused:
        await call_method(port, OBJECT, METHOD, None, Model(model_port, full()))

    assert str(refused.value) == message("methodNotExecutable", method_node_id=METHOD)
    assert port.calls == ["metadata"]


@pytest.mark.parametrize(
    ("declared", "given", "refused"),
    [(None, [1], False), ([], [1], True), ([{"dataType": "Double", "isArray": False}], [], True)],
    ids=["none-published", "empty-published", "one-declared"],
)
async def test_arguments_are_counted_only_against_published_input_arguments(
    declared, given, refused
):
    port = FakeMethodPort(declared)
    model = Model(
        FakeModelPort({OBJECT: object_facts(), METHOD: method_facts()}, on_object=True), full()
    )

    if refused:
        with pytest.raises(ApplicationRefusal, match="argument"):
            await call_method(port, OBJECT, METHOD, given, model)
        assert "call" not in port.calls
    else:
        result = await call_method(port, OBJECT, METHOD, given, model)
        assert result["status"] == "Good"


async def test_a_method_interlock_is_checked_after_the_plan(tmp_path):
    policy = operator(
        tmp_path,
        {
            "callable_methods": [{"object_id": OBJECT, "method_id": METHOD}],
            "preconditions": [
                {
                    "methods": [{"object_id": OBJECT, "method_id": METHOD}],
                    "require": [{"node": PERMIT, "equals": True}],
                }
            ],
        },
    )
    model_port = FakeModelPort(
        {OBJECT: object_facts(), METHOD: method_facts()},
        on_object=True,
        readings={PERMIT: {"status": "BadNotReadable"}},
    )
    port = FakeMethodPort(None)

    with pytest.raises(ApplicationRefusal) as refused:
        await call_method(port, OBJECT, METHOD, None, Model(model_port, policy))

    assert "unreadable (BadNotReadable)" in str(refused.value)
    assert "call" not in port.calls


# --- reads and history ------------------------------------------------------------


class FakeReadPort:
    async def values(self, node_ids):
        return [{"node_id": node_id, "write_access": None} for node_id in node_ids]

    async def engineering(self, node_ids):
        return {}


async def test_write_access_is_read_only_when_asked_for():
    model_port = FakeModelPort({SETPOINT: facts(), TEMPERATURE: facts(access_level=5)})

    plain = await read_nodes(FakeReadPort(), [SETPOINT], 10)
    asked = await read_nodes(FakeReadPort(), [SETPOINT, TEMPERATURE], 10, Model(model_port, full()))

    assert plain[0]["write_access"] is None
    assert asked[0]["write_access"]["allowed"] is True
    assert asked[0]["write_access"]["data_type"] == "Double"
    assert asked[1]["write_access"]["reason"] == CONTRACT["writeAccessReasons"]["readOnly"]
    assert model_port.calls == [["facts", [SETPOINT, TEMPERATURE]]]


class FailingHistoryPort:
    async def raw(self, node_id, start, end, wanted):
        raise AdapterFailure("history-raw", "BadHistoryOperationUnsupported", ValueError())


@pytest.mark.parametrize(("level", "hinted"), [(1, True), (5, False), (None, False)])
async def test_a_failed_history_read_says_when_the_node_keeps_no_history(level, hinted):
    model = Model(FakeModelPort({TEMPERATURE: facts(access_level=level)}), full())
    request = {"node_id": TEMPERATURE, "num_values": 10}

    with pytest.raises(AdapterFailure) as failed:
        await read_history(FailingHistoryPort(), request, [], model=model)

    hint = message("historyNotRecordedHint", node_id=TEMPERATURE)
    reason = "BadHistoryOperationUnsupported" + (f" {hint}" if hinted else "")
    assert str(failed.value) == message("historyFailed", node_id=TEMPERATURE, reason=reason)


# --- alarms -----------------------------------------------------------------------


class FakeAlarmPort:
    def __init__(self):
        self.calls = []

    def condition_for(self, event_id):
        return "ns=2;i=7"

    async def action(self, condition_id, event_id, action, comment, duration):
        self.calls.append("action")
        return {"status": "Good", "good": True}


@pytest.mark.parametrize(
    ("control", "condition", "expected"),
    [
        (
            {"alarm_sources": ["ns=2;i=5"]},
            {"SourceNode": ("value", "ns=2;i=6"), "Severity": ("value", 500)},
            message("alarmSourceNotAllowed", condition_id="ns=2;i=7", source_node_id="ns=2;i=6"),
        ),
        (
            {"alarm_max_severity": 400},
            {"SourceNode": ("error", "BadNoMatch"), "Severity": ("value", 500)},
            message("alarmTooSevere", condition_id="ns=2;i=7", severity=500, limit=400),
        ),
        (
            {"alarm_max_severity": 400},
            {"SourceNode": ("value", "ns=2;i=6"), "Severity": ("error", "BadNotReadable")},
            message(
                "alarmScopeUnreadable", condition_id="ns=2;i=7", reason="Severity: BadNotReadable"
            ),
        ),
        (
            {"alarm_sources": ["ns=2;i=6"], "alarm_max_severity": 600},
            {"SourceNode": ("value", "ns=2;i=6"), "Severity": ("value", 500)},
            None,
        ),
    ],
    ids=["source", "severity", "unreadable", "in-scope"],
)
async def test_an_alarm_outside_the_scope_is_not_acted_on(tmp_path, control, condition, expected):
    policy = operator(tmp_path, {"acknowledge_alarms": True, **control})
    port = FakeAlarmPort()
    model = Model(FakeModelPort(condition=condition), policy)

    async def acknowledge():
        return await act_on_alarm(port, "AQ==", "acknowledge", "", None, None, True, model)

    if expected is None:
        assert (await acknowledge())["status"] == "Good"
        assert port.calls == ["action"]
    else:
        with pytest.raises(ApplicationRefusal) as refused:
            await acknowledge()
        assert str(refused.value) == expected
        assert port.calls == []


# --- browse -----------------------------------------------------------------------


GRAPH = {
    "ns=2;i=1": [
        {"nodeId": "ns=2;i=118", "namespaceIndex": 2, "name": "Recipes", "nodeClass": "Object"},
        {"nodeId": SETPOINT, "namespaceIndex": 2, "name": "Setpoint", "nodeClass": "Variable"},
    ],
    "ns=2;i=118": [
        {"nodeId": "ns=2;i=119", "namespaceIndex": 2, "name": "Secret", "nodeClass": "Variable"}
    ],
}


class FakeBrowsePort:
    async def children(self, node_id):
        return GRAPH.get(node_id, [])

    async def enrich(self, records, include_values):
        self.enriched = [record["node_id"] for record in records]


def browse(port, hidden, **request):
    arguments = {
        "node_id": "ns=2;i=1",
        "browse_path": None,
        "depth": 2,
        "node_class": None,
        "name_filter": None,
        "include_values": False,
        "max_nodes": 100,
        **request,
    }
    return browse_nodes(port, hidden=hidden, **arguments)


async def test_a_browse_lists_nothing_deny_read_hides_and_reads_nothing_of_it():
    port = FakeBrowsePort()
    denied = {"ns=2;i=118", "ns=2;i=119"}

    result = await browse(port, denied.__contains__)

    assert [node["node_id"] for node in result["result"]["nodes"]] == [SETPOINT]
    # The walk is described as it was made.
    assert result["result"]["inspected"] == 3
    assert port.enriched == [SETPOINT]


async def test_a_browse_from_a_denied_start_named_by_path_is_refused():
    with pytest.raises(ApplicationRefusal) as refused:
        await browse(FakeBrowsePort(), {"ns=2;i=118"}.__contains__, browse_path="Recipes")
    assert str(refused.value) == message("readDenied", node_id="ns=2;i=118")
