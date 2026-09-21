"""What each alarm action calls, on what, with what (issue #119).

`acknowledge_alarm` implemented the first half of OPC UA Part 9 §5.5's
acknowledge→confirm handshake and nothing else, so an agent could say "I have
seen this" and then had no way to say "I have dealt with it", to leave a note, or
to do what an operator actually does with a chattering nuisance alarm.

This is where the *request* is pinned, because a fake client can be asked exactly
what it was handed and a real server cannot. The end-to-end tests in
`tests/e2e/test_events_e2e.py` cover the parts that need a real condition — and
in the shelving case they can only assert routing, because node-opcua implements
that state machine partly and unreliably. So this file carries the load.

`packages/server-node/test/alarm-actions.test.mjs` is the Node half and asserts
the same calls.

Two things here are easy to get wrong and do not fail cleanly:

**Which object.** The acknowledge family are methods of the condition's own type.
The three shelving ones are methods of ShelvedStateMachineType and hang off the
condition's `ShelvingState` component. Resolved against the wrong object, a server
finds a *different* method of the right name's neighbour and answers
`BadArgumentsMissing` or `BadTooManyArguments` — which is what this did before the
routing was fixed.

**Which node id.** The first draft used `ns=0;i=9211/9213/9215`, which are
`AlarmConditionType_ShelvingState_*` and are *not* in the order the names suggest.
The right fallbacks are ShelvedStateMachineType's own: Unshelve 2947,
OneShotShelve 2948, TimedShelve 2949.
"""

from __future__ import annotations

import pytest
from opcua import ua
from opcua_mcp_server.contract import EVENTS
from opcua_mcp_server.events import ACTIONS, acknowledge_alarm, alarm_action

CONDITION_ID = "ns=2;i=100"
EVENT_ID = "YWJjMTIz"  # base64 of "abc123"


class FakeNode:
    """A node that remembers what was called on it and what it was browsed for."""

    def __init__(self, node_id: str, children: dict[str, FakeNode] | None = None) -> None:
        self.node_id = node_id
        self._children = children or {}
        self.calls: list[tuple] = []

    # python-opcua's Node surface, only the parts the code under test touches.
    @property
    def nodeid(self):
        return type("NodeId", (), {"to_string": lambda _self: self.node_id})()

    def get_children(self):
        return list(self._children.values())

    def get_browse_name(self):
        name = self.node_id.rsplit(":", 1)[-1]
        return type("QualifiedName", (), {"Name": name})()

    def call_method(self, method, *args):
        self.calls.append((method, args))


class NamedNode(FakeNode):
    """A child whose browse name is given rather than derived from its id."""

    def __init__(self, browse_name: str, node_id: str = "ns=2;i=999") -> None:
        super().__init__(node_id)
        self._browse_name = browse_name

    def get_browse_name(self):
        return type("QualifiedName", (), {"Name": self._browse_name})()


class FakeClient:
    """Serves one condition, and records any well-known node asked for by id."""

    def __init__(self, condition: FakeNode) -> None:
        self.condition = condition
        self.requested: list[str] = []

    def get_node(self, node_id):
        if node_id == CONDITION_ID:
            return self.condition
        self.requested.append(node_id)
        return f"well-known:{node_id}"


def condition_with(*browse_names: str) -> FakeNode:
    """A condition instance exposing the named children."""
    return FakeNode(
        CONDITION_ID,
        {name: NamedNode(name, f"ns=2;i=1{index}") for index, name in enumerate(browse_names)},
    )


# --- the table itself ------------------------------------------------------------


def test_every_action_the_contract_declares_is_fully_specified():
    """A half-declared action would resolve to something, silently."""
    for name, spec in ACTIONS.items():
        assert set(spec) == {"browseName", "methodNodeId", "on", "takes", "description"}, name
        assert spec["on"] in {"condition", "shelvingState"}, name
        assert spec["takes"] in {"eventIdAndComment", "duration", "nothing"}, name
        assert spec["methodNodeId"].startswith("ns=0;i="), name


def test_the_shelving_actions_hang_off_the_shelving_state_and_the_rest_do_not():
    """The distinction that produced BadArgumentsMissing when it was missed."""
    assert {name for name, spec in ACTIONS.items() if spec["on"] == "shelvingState"} == {
        "shelve",
        "shelveFor",
        "unshelve",
    }
    assert {name for name, spec in ACTIONS.items() if spec["on"] == "condition"} == {
        "acknowledge",
        "confirm",
        "comment",
    }


@pytest.mark.parametrize(
    ("action", "node_id"),
    [
        # ShelvedStateMachineType's own methods, which are *not* numbered in the
        # order the names suggest — the first draft had all three wrong.
        ("unshelve", "ns=0;i=2947"),
        ("shelve", "ns=0;i=2948"),
        ("shelveFor", "ns=0;i=2949"),
        ("acknowledge", "ns=0;i=9111"),
        ("confirm", "ns=0;i=9113"),
        ("comment", "ns=0;i=9029"),
    ],
)
def test_the_well_known_fallback_ids_are_the_spec_s(action, node_id):
    assert ACTIONS[action]["methodNodeId"] == node_id


# --- what actually goes on the wire ----------------------------------------------


def test_the_acknowledge_family_passes_the_event_id_and_the_comment():
    condition = condition_with("Confirm")
    client = FakeClient(condition)

    alarm_action(client, CONDITION_ID, EVENT_ID, "confirm", "dealt with")

    [(method, args)] = condition.calls
    assert method is condition._children["Confirm"], "called the condition's own method"
    assert len(args) == 2
    assert args[0].Value == b"abc123", "the event id is decoded from base64 to a ByteString"
    assert args[0].VariantType == ua.VariantType.ByteString
    assert args[1].Value.Text == "dealt with"
    assert args[1].VariantType == ua.VariantType.LocalizedText


def test_acknowledge_alarm_is_the_same_call_by_another_name():
    """One implementation, two entry points — the property #119 had to preserve."""
    condition = condition_with("Acknowledge")
    client = FakeClient(condition)

    acknowledge_alarm(client, CONDITION_ID, EVENT_ID, "seen")

    [(method, args)] = condition.calls
    assert method is condition._children["Acknowledge"]
    assert args[0].Value == b"abc123"


def test_a_timed_shelve_sends_one_double_of_milliseconds():
    """Duration is a Double in OPC UA, not a struct.

    Sending it any other way is what answered `BadTooManyArguments`.
    """
    shelving = NamedNode(EVENTS["shelvingStateBrowseName"], "ns=2;i=200")
    shelving._children = {"TimedShelve": NamedNode("TimedShelve", "ns=2;i=201")}
    condition = FakeNode(CONDITION_ID, {"ShelvingState": shelving})
    client = FakeClient(condition)

    alarm_action(client, CONDITION_ID, EVENT_ID, "shelveFor", "", 30_000)

    assert condition.calls == [], "the condition itself must not be called"
    [(method, args)] = shelving.calls
    assert method is shelving._children["TimedShelve"]
    assert len(args) == 1, "TimedShelve takes exactly one argument"
    assert args[0].Value == 30_000.0
    assert args[0].VariantType == ua.VariantType.Double


@pytest.mark.parametrize("action", ["shelve", "unshelve"])
def test_the_argument_less_shelving_actions_send_nothing(action):
    browse_name = ACTIONS[action]["browseName"]
    shelving = NamedNode(EVENTS["shelvingStateBrowseName"], "ns=2;i=200")
    shelving._children = {browse_name: NamedNode(browse_name, "ns=2;i=201")}
    condition = FakeNode(CONDITION_ID, {"ShelvingState": shelving})
    client = FakeClient(condition)

    alarm_action(client, CONDITION_ID, EVENT_ID, action)

    [(method, args)] = shelving.calls
    assert method is shelving._children[browse_name]
    assert args == (), f"{browse_name} takes no arguments"


def test_a_condition_with_no_shelving_state_is_refused_and_told_why():
    """ShelvingState is optional in Part 9, so a server without it is conformant.

    Falling back to the type node would be worse than useless: shelving is
    per-instance state, and a call against the type would either fail obscurely or
    change something nobody asked about.
    """
    client = FakeClient(condition_with("Acknowledge"))

    with pytest.raises(ValueError, match="has no ShelvingState"):
        alarm_action(client, CONDITION_ID, EVENT_ID, "shelve")


def test_a_condition_that_hides_its_methods_falls_back_to_the_type_s():
    """Part 9 allows a server not to expose condition instances at all.

    The type's own method is then called with the condition as the object, which
    is what `acknowledge_alarm` has always done and what the rest now do too.
    """
    condition = condition_with()  # no children at all
    client = FakeClient(condition)

    alarm_action(client, CONDITION_ID, EVENT_ID, "comment", "note")

    [(method, _)] = condition.calls
    assert method == f"well-known:{ACTIONS['comment']['methodNodeId']}"
    assert client.requested == [ACTIONS["comment"]["methodNodeId"]]
