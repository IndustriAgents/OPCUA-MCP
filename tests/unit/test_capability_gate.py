"""A capability gates the call, never the catalogue (issue #140).

The catalogue used to depend on what the connected server supported, so a server
started while the plant was down advertised less than one started while it was
up — and a client that listed once, as most do, kept the smaller list. These pin
the replacement: every tool advertised whatever the answers are, the call decided
against the answers for *its* session, and the refusal worded identically on both
runtimes.

``packages/server-node/test/capability-gate.test.mjs`` is the Node half and drives
the same table, ``tests/fixtures/capability-gate.json``. Needs no OPC UA server.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua import ua
from opcua_mcp_server import server as server_module
from opcua_mcp_server.capabilities import (
    CAPABILITY_NAMES,
    CapabilityAnswers,
    Probe,
    Verdict,
    answers_from,
    capability_status,
    client_aggregate_functions,
    client_supports_history,
    refusal,
    requirements,
    verdict,
)
from opcua_mcp_server.connection import is_connection_error
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.server import create_server
from opcua_mcp_server.state import ServerState

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "capability-gate.json").read_text(encoding="utf-8")
)
SPECS = {tool["name"]: tool for tool in CONTRACT["tools"]}


@pytest.mark.parametrize("case", FIXTURE["decisions"], ids=lambda case: case["name"])
def test_the_shared_table_decides_each_call(case):
    decided = verdict(requirements(SPECS[case["tool"]], case["arguments"]), case["support"])
    assert decided.outcome == case["expected"]["outcome"]
    assert list(decided.capabilities) == case["expected"]["capabilities"]


def _answers(raw: dict) -> CapabilityAnswers:
    fields = {
        "generation": raw["generation"],
        "checked_at": raw["checked_at"],
        "support": raw["support"],
    }
    if raw.get("reasons") is not None:
        fields["reasons"] = raw["reasons"]
    if "aggregate_functions" in raw:
        fields["aggregate_functions"] = dict.fromkeys(raw["aggregate_functions"])
    return CapabilityAnswers(**fields)


@pytest.mark.parametrize("case", FIXTURE["messages"], ids=lambda case: case["name"])
def test_the_refusal_is_worded_as_the_shared_table_words_it(case):
    decided = Verdict(case["verdict"]["outcome"], tuple(case["verdict"]["capabilities"]))
    text = refusal(case["tool"], decided, _answers(case["answers"]), case["url"])
    assert text == case["expected"]
    # The code first, so a client need not parse prose to know which it got.
    assert text.startswith(f"{case['verdict']['outcome']}: ")


@pytest.mark.parametrize("case", FIXTURE["status"], ids=lambda case: case["name"])
def test_the_status_record_is_the_shared_one(case):
    assert capability_status(_answers(case["answers"])) == case["expected"]


# --- the probes: an answer, or no answer -------------------------------------------


class _Node:
    def __init__(self, outcome):
        self._outcome = outcome

    def get_value(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    def get_referenced_nodes(self, **_):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        # A boolean is a node that answers a read; as a folder, it is empty.
        return [] if isinstance(self._outcome, bool) else self._outcome


class _Client:
    def __init__(self, outcome):
        self._outcome = outcome

    def get_node(self, _node_id):
        return _Node(self._outcome)


@pytest.mark.parametrize(
    ("outcome", "support"),
    [
        (True, "supported"),
        (False, "not_supported"),
        # The node does not exist: the server answered, and the answer is no.
        (ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown), "not_supported"),
        # The session is gone, or the read never came back: nobody answered.
        (ua.UaStatusCodeError(ua.StatusCodes.BadSessionIdInvalid), "unknown"),
        (TimeoutError(), "unknown"),
        (ConnectionResetError(54, "Connection reset by peer"), "unknown"),
    ],
    ids=["true", "false", "absent", "dead-session", "timeout", "reset"],
)
def test_a_probe_tells_a_no_from_no_answer(outcome, support):
    probe = client_supports_history(_Client(outcome))
    assert probe.support == support
    if support == "unknown":
        assert probe.reason.startswith("reading AccessHistoryDataCapability failed: ")


def test_an_aggregate_folder_with_nothing_known_in_it_is_a_no():
    probe, functions = client_aggregate_functions(_Client([]))
    assert (probe.support, functions) == ("not_supported", {})
    probe, functions = client_aggregate_functions(_Client(TimeoutError()))
    assert probe.support == "unknown"


# --- tools/list does not depend on the answers ----------------------------------------


def _all(support: str) -> CapabilityAnswers:
    return answers_from(
        1, "2026-09-24T10:00:00.000Z", {name: Probe(support) for name in CAPABILITY_NAMES}, {}
    )


def _definitions(tools) -> list[dict]:
    return [tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in tools]


async def test_the_catalogue_is_the_same_whatever_the_server_supports():
    """Nothing asked, everything supported, nothing supported: one catalogue.

    The old behaviour, for contrast: the first withheld `read_event_history` and
    `read_opcua_history` and the last withheld `aggregate_function` too.
    """
    mcp = create_server(ServerState(url="opc.tcp://127.0.0.1:1/none"))
    catalogues = []
    for answers in (CapabilityAnswers(), _all("supported"), _all("not_supported")):
        mcp.state.capabilities = answers
        catalogues.append(_definitions(await mcp.list_tools()))
    assert catalogues[0] == catalogues[1] == catalogues[2]

    history = next(t for t in catalogues[0] if t["name"] == "read_opcua_history")
    assert history["inputSchema"] == SPECS["read_opcua_history"]["inputSchema"]


# --- a call is checked against the answers for its own session ----------------------


def _gate(monkeypatch, *, cached: CapabilityAnswers, generation: int, fresh: CapabilityAnswers):
    """A server whose connection is on `generation`, holding `cached` answers.

    Asking the server is replaced by one that records it was asked and answers
    `fresh`.
    """
    mcp = create_server(ServerState(url="opc.tcp://plc:4840"))
    mcp.state.capabilities = cached
    probes = []

    def probe(state, connection):
        probes.append(connection.session_generation)
        state.capabilities = fresh
        return fresh

    monkeypatch.setattr(server_module, "_fresh_capabilities", probe)
    connection = SimpleNamespace(
        session_generation=generation, client=object(), url="opc.tcp://plc:4840"
    )
    return mcp, connection, probes


def _on(generation: int, **support: str) -> CapabilityAnswers:
    return answers_from(
        generation,
        "2026-09-24T10:00:00.000Z",
        {name: Probe(support.get(name, "not_supported")) for name in CAPABILITY_NAMES},
        {},
    )


async def test_answers_from_an_older_session_are_asked_again(monkeypatch):
    """The server restarted without history: the old yes must not let the call through."""
    mcp, connection, probes = _gate(
        monkeypatch,
        cached=_on(1, history="supported"),
        generation=2,
        fresh=_on(2, history="not_supported"),
    )
    with pytest.raises(ToolError, match=r"^capability_not_supported: read_opcua_history "):
        await mcp._ensure_capabilities(SPECS["read_opcua_history"], {"node_id": "x"}, connection)
    assert probes == [2]


async def test_answers_from_this_session_are_not_asked_again(monkeypatch):
    mcp, connection, probes = _gate(
        monkeypatch,
        cached=_on(2, history="supported"),
        generation=2,
        fresh=_on(2),
    )
    await mcp._ensure_capabilities(SPECS["read_opcua_history"], {"node_id": "x"}, connection)
    assert probes == []


async def test_an_unknown_answer_is_asked_again(monkeypatch):
    """A probe that could not finish last time may well finish now."""
    mcp, connection, probes = _gate(
        monkeypatch,
        cached=_on(2, historyEvents="unknown"),
        generation=2,
        fresh=_on(2, historyEvents="supported"),
    )
    await mcp._ensure_capabilities(SPECS["read_event_history"], {}, connection)
    assert probes == [2]


async def test_a_cached_no_is_confirmed_with_the_server_before_refusing(monkeypatch):
    """A "no" from the cache would refuse without touching the network.

    So it could never find out that the server had come back with the feature —
    python-opcua cannot tell a dead socket from a live one until it is used, and
    a refused call uses nothing. The e2e restart test is where this was found.
    """
    mcp, connection, probes = _gate(
        monkeypatch,
        cached=_on(2, history="not_supported"),
        generation=2,
        fresh=_on(2, history="supported"),
    )
    await mcp._ensure_capabilities(SPECS["read_opcua_history"], {"node_id": "x"}, connection)
    assert probes == [2]


async def test_a_server_that_cannot_be_asked_is_an_outage_not_a_refusal(monkeypatch):
    mcp = create_server(ServerState(url="opc.tcp://plc:4840"))
    mcp.state.capabilities = _on(1)

    def unreachable(state, connection):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(server_module, "_fresh_capabilities", unreachable)
    connection = SimpleNamespace(session_generation=1, client=object(), url="opc.tcp://plc:4840")
    with pytest.raises(ToolError, match=r"^endpoint_offline: "):
        await mcp._ensure_capabilities(SPECS["read_event_history"], {}, connection)


class _Rebuilding:
    """A connection whose first client has died, run the way ``OpcuaConnection.run`` runs.

    One attempt; on a dead-session error, rebuild onto the next client and try
    once more.
    """

    def __init__(self, *clients):
        self._clients = list(clients)
        self.client = self._clients.pop(0)
        self.session_generation = 1
        self.rebuilds = 0

    def run(self, operation):
        try:
            return operation()
        except Exception as error:
            if not is_connection_error(error):
                raise
            self.rebuilds += 1
            self.client = self._clients.pop(0)
            self.session_generation += 1
            return operation()


def test_asking_again_rebuilds_a_session_that_died_under_the_probe():
    state = ServerState(url="opc.tcp://plc:4840")
    dead = _Client(ua.UaStatusCodeError(ua.StatusCodes.BadSessionIdInvalid))
    connection = _Rebuilding(dead, _Client(True))

    answers = server_module._fresh_capabilities(state, connection)

    assert connection.rebuilds == 1
    assert answers.generation == 2
    assert answers.support["history"] == "supported"
    assert state.capabilities is answers


async def test_an_ungated_call_asks_nothing(monkeypatch):
    mcp, connection, probes = _gate(
        monkeypatch, cached=CapabilityAnswers(), generation=7, fresh=_on(7)
    )
    await mcp._ensure_capabilities(SPECS["read_opcua_nodes"], {"node_ids": ["x"]}, connection)
    assert probes == []
