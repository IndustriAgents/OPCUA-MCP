"""What an allowlisted node may be *written*, not only which node it is (#109, #110).

Node identity was the whole of write authorization, and it is the weakest link in
the safety story: a model that correctly identified the right setpoint and
hallucinated ``9999`` instead of ``99.9`` was fully authorized. The variant codec
range-checks integers and refuses a lossy Int64, but that is *type* safety —
``9999`` is a perfectly good Double.

Three bounds, in two places, and the split is deliberate:

* ``min``, ``max`` and ``enum`` are decidable from the call alone, so they are
  checked by the policy layer before a single byte reaches the plant. The shared
  table in ``tests/fixtures/value-bounds.json`` drives them, and
  ``packages/server-node/test/value-bounds.test.mjs`` drives the same table.
* ``max_change`` is a bound on the *move*, so it needs the node's current value
  and lives in the write path. Covered here directly.
* The server's own ``EURange`` is better than any of them — the plant published
  it — and is covered end to end in ``tests/e2e/test_policy_e2e.py``, because it
  needs a server that actually publishes one.
"""

from __future__ import annotations

import json
import tempfile

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua_mcp_server.node_metadata import (
    MAX_PER_REQUEST,
    AnalogInfo,
    NodeMetadata,
    Range,
)
from opcua_mcp_server.policy import (
    ToolPolicy,
    ValueBound,
    as_number,
    format_number,
    pairs_at,
    parse_policy_config,
)
from opcua_mcp_server.server import check_eu_range, check_max_change

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "value-bounds.json").read_text(encoding="utf-8")
)
CASES = FIXTURE["cases"]

_WRITE_GUARD = {"array": "nodes", "nodeIdField": "node_id", "valueField": "value"}


def _write_policy(document: dict) -> str:
    """A policy file on disk, because that is the only way to express a bound."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump({"version": 1, **document}, handle)
        return handle.name


def policy_from_fixture() -> ToolPolicy:
    """The shared table's policy, as an operator deployment on a secured channel."""
    spec = FIXTURE["policy"]
    config = parse_policy_config(
        {
            "OPCUA_PROFILE": spec["profile"],
            "OPCUA_SECURITY_POLICY": "Basic256Sha256" if spec["secure"] else "None",
            "OPCUA_POLICY_FILE": _write_policy(
                {"control": {"writable_nodes": spec["writable_nodes"]}}
            ),
        }
    )
    subject = ToolPolicy(config)
    subject.bind_namespaces(["http://opcfoundation.org/UA/", "urn:one", "urn:plant"])
    return subject


@pytest.fixture(scope="module")
def subject() -> ToolPolicy:
    return policy_from_fixture()


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_policy_answers_the_shared_table(case, subject):
    """The Node half asserts the same verdict and the same sentence."""
    arguments = {"nodes": [{"node_id": case["node_id"], "value": case["value"]}]}
    if case["error"] is None:
        subject.authorize("write_opcua_nodes", arguments)
        return
    with pytest.raises((PermissionError, ValueError)) as raised:
        subject.authorize("write_opcua_nodes", arguments)
    assert str(raised.value) == case["error"]


def test_a_batch_is_refused_whole(subject):
    """One bad value rejects every write in the call, as one bad target already did.

    A batch that applied the acceptable half and refused the rest would leave the
    plant in a state nobody asked for and no record of which half landed.
    """
    with pytest.raises(PermissionError):
        subject.authorize(
            "write_opcua_nodes",
            {
                "nodes": [
                    {"node_id": "ns=2;i=1", "value": 1},
                    {"node_id": "ns=2;i=2", "value": 9999},
                ]
            },
        )


def test_a_bound_follows_a_renumbered_server():
    """An ``nsu=`` bound has to resolve like an ``nsu=`` allowlist entry does.

    A bound that only matched one spelling of a node id would be a bound an
    operator believes is in force and is not — and the whole point of the ``nsu=``
    form is that the index moves when the server restarts.
    """
    config = parse_policy_config(
        {
            "OPCUA_PROFILE": "operator",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_POLICY_FILE": _write_policy(
                {"control": {"writable_nodes": [{"node": "nsu=urn:plant;i=5", "max": 100}]}}
            ),
        }
    )
    subject = ToolPolicy(config)

    subject.bind_namespaces(["http://opcfoundation.org/UA/", "urn:other", "urn:plant"])
    with pytest.raises(PermissionError):
        subject.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=5", "value": 500}]})

    # The server restarts and loads its namespaces in a different order. The
    # bound has to move with the URI, not stay at index 2.
    subject.bind_namespaces(["http://opcfoundation.org/UA/", "urn:plant", "urn:other"])
    with pytest.raises(PermissionError):
        subject.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=1;i=5", "value": 500}]})
    subject.authorize("write_opcua_nodes", {"nodes": [{"node_id": "ns=1;i=5", "value": 50}]})


# --- how a policy file is read ---------------------------------------------------


def test_a_bare_node_id_stays_legal():
    """Every policy file written before bounds existed means what it meant."""
    config = parse_policy_config(
        {
            "OPCUA_PROFILE": "operator",
            "OPCUA_POLICY_FILE": _write_policy(
                {"control": {"writable_nodes": ["ns=2;i=1", {"node": "ns=2;i=2", "max": 10}]}}
            ),
        }
    )
    assert config.writable_nodes == frozenset({"ns=2;i=1", "ns=2;i=2"})
    assert config.value_bounds["ns=2;i=1"] == ValueBound()
    assert config.value_bounds["ns=2;i=2"].maximum == 10.0


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"node": "ns=2;i=1", "minimum": 0}, "Unknown key"),
        ({"min": 0}, "must name a node"),
        ({"node": "ns=2;i=1", "min": "cold"}, "non-numeric min"),
        ({"node": "ns=2;i=1", "min": 10, "max": 0}, "min above max"),
        ({"node": "ns=2;i=1", "enum": []}, "empty or non-list enum"),
        ({"node": "ns=2;i=1", "max_change": -1}, "negative max_change"),
        (42, "must be a node id or an object"),
    ],
)
def test_a_malformed_bound_is_refused_at_startup(entry, reason):
    """Loud, because the failure it prevents is silent.

    An operator who writes ``"minimum"`` instead of ``"min"`` believes a bound is
    in force and none is — and finds out when a write that should have been
    refused reaches the plant.
    """
    with pytest.raises(ValueError, match=reason):
        parse_policy_config(
            {
                "OPCUA_PROFILE": "operator",
                "OPCUA_POLICY_FILE": _write_policy({"control": {"writable_nodes": [entry]}}),
            }
        )


def test_the_environment_variable_carries_node_ids_only():
    """A comma-separated list cannot express a bound, and does not pretend to."""
    config = parse_policy_config(
        {
            "OPCUA_PROFILE": "operator",
            "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=1,ns=2;i=2",
            "OPCUA_POLICY_FILE": _write_policy(
                {"control": {"writable_nodes": [{"node": "ns=2;i=9", "max": 10}]}}
            ),
        }
    )
    # The variable replaces the file's list outright rather than merging: a
    # half-overridden allowlist is the kind of thing nobody can reason about at
    # three in the morning.
    assert config.writable_nodes == frozenset({"ns=2;i=1", "ns=2;i=2"})
    assert all(bound.is_empty for bound in config.value_bounds.values())


# --- the helpers the two runtimes share ------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42.0),
        (42.5, 42.5),
        ("42.5", 42.5),
        ("  42.5  ", 42.5),
        ("", None),
        ("warm", None),
        (True, None),
        (False, None),
        (None, None),
        ([1], None),
    ],
)
def test_as_number(value, expected):
    assert as_number(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(100.0, "100"), (100, "100"), (-20.5, "-20.5"), (0.1, "0.1"), (float("inf"), "infinity")],
)
def test_format_number_reads_the_way_a_refusal_should(value, expected):
    """``formatNumber`` in ``policy.ts`` must agree character for character."""
    assert format_number(value) == expected


def test_pairs_at_keeps_each_target_with_its_own_value():
    """Flattening would authorise a value against the wrong node's bound."""
    arguments = {
        "nodes": [
            {"node_id": "ns=2;i=1", "value": 1},
            {"node_id": "ns=2;i=2", "value": 2},
        ]
    }
    assert pairs_at(arguments, _WRITE_GUARD) == [("ns=2;i=1", 1), ("ns=2;i=2", 2)]


def test_pairs_at_skips_an_element_with_no_node_id():
    """Not a hole: the schema requires it and the allowlist has already refused it."""
    assert pairs_at({"nodes": [{"value": 1}]}, _WRITE_GUARD) == []
    assert pairs_at({"nodes": "not a list"}, _WRITE_GUARD) == []


# --- what the plant itself published ---------------------------------------------


def test_a_range_is_inclusive_at_both_ends():
    """OPC UA's Range is, so a value sitting exactly on the limit is in it."""
    eu_range = Range(0.0, 100.0)
    assert eu_range.contains(0.0)
    assert eu_range.contains(100.0)
    assert not eu_range.contains(100.000001)


def test_a_node_that_published_nothing_is_not_a_record():
    """Most nodes are not AnalogItems, and `engineering: {}` would say they were."""
    assert AnalogInfo().is_empty
    assert not AnalogInfo(unit="°C").is_empty
    assert AnalogInfo(eu_range=Range(0, 1)).to_json()["eu_range"] == {"low": 0, "high": 1}


# --- max_change, and the EURange the plant published -----------------------------
#
# Both need a read, so they live in the write path rather than in the policy
# layer, and both refuse the whole batch before anything is sent.


class _Status:
    """Just enough of a python-opcua StatusCode for the checks to read one."""

    def __init__(self, name: str, good: bool) -> None:
        self.name = name
        self._good = good

    def is_good(self) -> bool:
        return self._good


class _DataValue:
    """Just enough of a python-opcua DataValue for the checks to read one."""

    def __init__(self, value, status: str = "Good") -> None:
        self.Value = _Variant(value)
        self.StatusCode = _Status(status, status == "Good")


class _Variant:
    def __init__(self, value) -> None:
        self.Value = value
        self.VariantType = None
        self.is_array = False


ANALOG = AnalogInfo(
    unit="°C",
    unit_description="degree Celsius",
    eu_range=Range(0.0, 150.0),
    instrument_range=Range(-50.0, 250.0),
)


def test_a_value_the_plant_says_is_normal_is_allowed():
    check_eu_range("ns=2;i=90", 51.75, ANALOG)
    check_eu_range("ns=2;i=90", "51.75", ANALOG)


def test_a_value_outside_the_servers_own_range_is_refused():
    """The bound nobody had to type into a policy file, and the better one for it."""
    with pytest.raises(ToolError) as raised:
        check_eu_range("ns=2;i=90", 200, ANALOG)
    assert str(raised.value) == (
        "200 is outside the range node ns=2;i=90 accepts (0 to 150 °C), set by the "
        "OPC UA server's own EURange. Nothing was written. Read the node to see where "
        "it is now, or widen the bound if this is deliberate."
    )


def test_a_node_with_no_published_range_is_not_bounded_by_one():
    check_eu_range("ns=2;i=41", 999999, None)
    check_eu_range("ns=2;i=41", 999999, AnalogInfo(unit="°C"))


def test_a_non_numeric_write_is_left_to_the_codec():
    """A range comparison says nothing useful about "warm"; the codec says it better."""
    check_eu_range("ns=2;i=90", "warm", ANALOG)
    check_eu_range("ns=2;i=90", True, ANALOG)


def test_a_move_inside_the_limit_is_allowed():
    check_max_change("ns=2;i=90", 55, ValueBound(max_change=10), _DataValue(50.0))
    # Exactly the limit is inside it: the bound is "at most this far".
    check_max_change("ns=2;i=90", 60, ValueBound(max_change=10), _DataValue(50.0))


def test_a_move_larger_than_the_limit_is_refused():
    with pytest.raises(ToolError) as raised:
        check_max_change("ns=2;i=90", 140, ValueBound(max_change=10), _DataValue(50.0))
    assert str(raised.value) == (
        "Moving node ns=2;i=90 from 50 to 140 is a change of 90, and the operator policy "
        "allows at most 10 in one write. Nothing was written. Make the move in steps if "
        "it is deliberate."
    )


def test_a_move_is_measured_in_both_directions():
    with pytest.raises(ToolError):
        check_max_change("ns=2;i=90", 10, ValueBound(max_change=10), _DataValue(50.0))


def test_a_move_cannot_be_judged_without_knowing_where_the_node_is():
    """Refused rather than waved through: an unenforceable bound is not a bound.

    This is also why a `max_change` on a write-only node cannot work — reading it
    is exactly what such a node refuses, which is the whole reason `data_type`
    exists on a write request.
    """
    with pytest.raises(ToolError, match="it could not be read"):
        check_max_change("ns=2;i=90", 55, ValueBound(max_change=10), None)
    with pytest.raises(ToolError, match="BadNotReadable"):
        check_max_change(
            "ns=2;i=90", 55, ValueBound(max_change=10), _DataValue(None, "BadNotReadable")
        )


def test_an_array_has_no_single_distance_to_have_moved():
    """Guessing one would be a rule nobody could predict from the policy file."""
    with pytest.raises(ToolError, match="only accepts a number"):
        check_max_change("ns=2;i=90", [51, 52], ValueBound(max_change=10), _DataValue(50.0))


# --- how the properties are actually fetched -------------------------------------


class _FakeUaClient:
    """Records what was asked for, and answers with nothing.

    Enough to see the *shape* of the requests — how many, and how large — which
    is the part a live server cannot be relied on to object to until it is a real
    plant with a real operational limit.
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.translate_sizes: list[int] = []
        self.read_sizes: list[int] = []
        self.fail = fail

    def translate_browsepaths_to_nodeids(self, paths):
        if self.fail is not None:
            raise self.fail
        self.translate_sizes.append(len(paths))
        return [_NoMatch() for _ in paths]

    def get_attributes(self, node_ids, attribute):
        self.read_sizes.append(len(node_ids))
        return []


class _NoMatch:
    """A browse path that resolved to nothing, which is most of them."""

    def __init__(self) -> None:
        self.StatusCode = _Status("BadNoMatch", good=False)
        self.Targets: list = []


class _FakeClient:
    def __init__(self, fail: Exception | None = None) -> None:
        self.uaclient = _FakeUaClient(fail)

    def get_node(self, node_id):
        return type("Node", (), {"nodeid": node_id})()


def test_a_large_batch_is_split_into_requests_a_server_will_accept():
    """500 nodes is 1500 browse paths, and a server may refuse one request that big.

    ``MaxNodesPerTranslateBrowsePathsToNodeIds`` is an operational limit servers
    publish and enforce. Nothing in the mock does, which is exactly why this is
    asserted on the request shape rather than left to an end-to-end test to
    discover against someone's real plant.
    """
    client = _FakeClient()
    NodeMetadata().for_nodes(client, [f"ns=2;i={index}" for index in range(500)])

    assert sum(client.uaclient.translate_sizes) == 1500
    assert max(client.uaclient.translate_sizes) <= MAX_PER_REQUEST
    assert len(client.uaclient.translate_sizes) == 5


def test_a_server_that_cannot_answer_the_question_is_not_asked_again():
    """A server without TranslateBrowsePathsToNodeIds will not learn to have it.

    Asking on every read would cost a round trip and a line of stderr each time,
    for the whole life of the session.
    """
    client = _FakeClient(fail=ValueError("BadServiceUnsupported"))
    metadata = NodeMetadata()

    assert metadata.for_nodes(client, ["ns=2;i=1"]) == {"ns=2;i=1": None}
    assert metadata.for_nodes(client, ["ns=2;i=2"]) == {"ns=2;i=2": None}

    # The second call did not reach the server at all.
    assert client.uaclient.translate_sizes == []

    # And a new session starts the question over: it may be a different server.
    metadata.forget()
    client.uaclient.fail = None
    metadata.for_nodes(client, ["ns=2;i=3"])
    assert client.uaclient.translate_sizes == [3]


def test_a_connection_failure_is_not_remembered_as_an_answer():
    """One bad moment must not leave a whole session's readings unqualified."""
    client = _FakeClient(fail=ConnectionResetError("ECONNRESET"))
    metadata = NodeMetadata()

    assert metadata.for_nodes(client, ["ns=2;i=1"]) == {"ns=2;i=1": None}
    client.uaclient.fail = None
    metadata.for_nodes(client, ["ns=2;i=1"])

    assert client.uaclient.translate_sizes == [3], "the retry never happened"
