"""What the OPC UA server can do, and what a call that needs it is told (#140).

A capability used to decide what ``tools/list`` *said*: ``read_event_history``
was missing from the catalogue of a server without an event archive, and
``read_opcua_history`` lost its ``aggregate_function`` argument on one without
aggregates. That made plant availability part of the MCP interface. A server
started while the plant was down listed neither, and when the plant came back
nothing portable told the client to list again — clients and models commonly
hold a tool list for the life of a session, and ``notifications/tools/list_changed``
cannot be sent the same way by both SDKs (docs/architecture.md). So the catalogue
is now the contract, always, and the capability is checked where it can actually
be known: on the call, against the live session.

The answers are cached per OPC UA session. A new session is a new *generation*,
and answers read on an older one are never trusted: a restarted server may not be
the same server. ``get_server_status`` reports the cache as it stands, with the
generation and time it was read.

``capabilities.ts`` is the Node half, and ``tests/fixtures/capability-gate.json``
pins the two to the same decisions and the same words.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from opcua import ua

from .aggregates import spec_aggregate_node_ids
from .connection import is_connection_error
from .contract import AGGREGATE_NODE_ID, CONTRACT, HISTORY_EVENTS_NODE_ID, HISTORY_NODE_ID
from .errors import message

#: One answer: the server said yes, the server said no, or nobody could ask it.
Support = Literal["supported", "not_supported", "unknown"]

#: The capabilities the contract defines, in its order. ``$`` keys are prose.
CAPABILITIES: dict[str, dict[str, Any]] = {
    name: spec for name, spec in CONTRACT["capabilities"].items() if not name.startswith("$")
}
CAPABILITY_NAMES: tuple[str, ...] = tuple(CAPABILITIES)

#: Why an answer is ``unknown`` before any session has been asked.
NOT_YET_ASKED = "not yet asked: no OPC UA session has been established"


@dataclass(frozen=True)
class Probe:
    """What one probe found, and why it found nothing when it could not ask."""

    support: Support
    reason: str | None = None
    #: What stopped an ``unknown`` probe, so a caller can tell a session that
    #: died under it — worth rebuilding and asking again — from one that timed out.
    error: BaseException | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class CapabilityAnswers:
    """Everything known about the server's optional features, for one session.

    Frozen and replaced whole, never updated in place: the probes run in a worker
    thread while calls read these on the event loop, and a reader must see one
    session's answers or the next one's, never half of each.
    """

    #: The session generation these were read on; None before any was asked.
    generation: int | None = None
    #: When, ISO-8601 UTC; None before any session was asked.
    checked_at: str | None = None
    support: Mapping[str, Support] = field(
        default_factory=lambda: dict.fromkeys(CAPABILITY_NAMES, "unknown")
    )
    #: Why each ``unknown`` is unknown.
    reasons: Mapping[str, str] = field(
        default_factory=lambda: dict.fromkeys(CAPABILITY_NAMES, NOT_YET_ASKED)
    )
    #: The aggregate functions the server offers, mapped to the node IDs a
    #: ``ReadProcessedDetails`` request needs.
    aggregate_functions: Mapping[str, ua.NodeId] = field(default_factory=dict)
    #: The probes these were assembled from; empty before any session was asked.
    probes: Mapping[str, Probe] = field(default_factory=dict, compare=False, repr=False)


def answers_from(
    generation: int | None,
    checked_at: str,
    probes: Mapping[str, Probe],
    aggregate_functions: Mapping[str, ua.NodeId],
) -> CapabilityAnswers:
    """Assemble one session's answers from its probes."""
    support: dict[str, Support] = {}
    reasons: dict[str, str] = {}
    for name in CAPABILITY_NAMES:
        probe = probes.get(name, Probe("unknown", NOT_YET_ASKED))
        support[name] = probe.support
        if probe.support == "unknown":
            reasons[name] = probe.reason or "no answer"
    return CapabilityAnswers(
        generation=generation,
        checked_at=checked_at,
        support=support,
        reasons=reasons,
        aggregate_functions=dict(aggregate_functions),
        probes=dict(probes),
    )


def now_iso_utc() -> str:
    """Now, as ISO-8601 UTC in milliseconds — ``Date.toISOString()`` on Node."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def requirements(tool: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[list[str]]:
    """What a call needs, as groups of which each must have one capability supported.

    The tool's own ``capabilities`` are one group; each gated argument the call
    actually passes adds another. ``read_opcua_history`` needs history *or*
    aggregates, and with ``aggregate_function`` it needs aggregates as well —
    which is what hiding the argument used to express.
    """
    groups = [list(tool["capabilities"])] if tool["capabilities"] else []
    for argument, needs in (tool.get("argumentCapabilities") or {}).items():
        if arguments.get(argument) is not None:
            groups.append(list(needs))
    return groups


@dataclass(frozen=True)
class Verdict:
    """Whether a call may go ahead, and if not, which group stopped it and why."""

    outcome: Literal["allowed", "capability_not_supported", "capability_unknown"]
    capabilities: tuple[str, ...] = ()


def verdict(groups: list[list[str]], support: Mapping[str, Support]) -> Verdict:
    """Decide a call from its requirement groups and the answers.

    A group is met by any one supported member. One that is not met is
    ``unknown`` if any member is unknown — the server may yet say yes, and
    refusing it as unsupported would be stating something nobody found out — and
    otherwise ``not_supported``.
    """
    for group in groups:
        if any(support.get(name) == "supported" for name in group):
            continue
        unknown = any(support.get(name) != "not_supported" for name in group)
        return Verdict(
            "capability_unknown" if unknown else "capability_not_supported", tuple(group)
        )
    return Verdict("allowed")


def _requirement(capabilities: tuple[str, ...]) -> str:
    """What a refusal says is needed: "historical data access (…, ns=0;i=11193) or …"."""
    return " or ".join(
        f"{CAPABILITIES[name]['label']} ({CAPABILITIES[name]['browseName']}, "
        f"{CAPABILITIES[name]['nodeId']})"
        for name in capabilities
    )


def refusal(tool: str, decided: Verdict, answers: CapabilityAnswers, url: str) -> str:
    """The refusal for a call :func:`verdict` did not allow, from the contract.

    The remediation is the first capability's: a group is listed most useful
    first, so for a server with neither history nor aggregates the advice is
    about history, which is what a raw read — the common call — needed.
    """
    generation = answers.generation if answers.generation is not None else "none"
    requirement = _requirement(decided.capabilities)
    if decided.outcome == "capability_not_supported":
        return message(
            "capabilityNotSupported",
            tool=tool,
            requirement=requirement,
            url=url,
            generation=generation,
            checked_at=answers.checked_at or "never",
            remediation=CAPABILITIES[decided.capabilities[0]]["remediation"],
        )
    unknown = next(
        (name for name in decided.capabilities if answers.support.get(name) != "not_supported"),
        None,
    )
    return message(
        "capabilityUnknown",
        tool=tool,
        requirement=requirement,
        url=url,
        generation=generation,
        reason=(answers.reasons.get(unknown) if unknown else None) or NOT_YET_ASKED,
    )


def capability_status(answers: CapabilityAnswers) -> dict[str, Any]:
    """``serverStatus.capabilities``: the cache as it stands, and which session it is from."""
    return {
        "session_generation": answers.generation,
        "checked_at": answers.checked_at,
        "support": {name: answers.support[name] for name in CAPABILITY_NAMES},
        "aggregate_functions": list(answers.aggregate_functions),
    }


# --- the probes ------------------------------------------------------------------


def _unanswered(error: BaseException) -> bool:
    """True when a probe failed without the server having answered it.

    A status code for the *node* — it does not exist, it may not be read — is the
    server's answer, and means ``not_supported``. A dead session, a timeout or a
    socket error is not an answer at all, and a timeout reported as "the server
    said no" refused a call the next attempt could have served. The Node half
    draws the same line where node-opcua does: a per-operation status comes back
    in the DataValue, a failure of the request is thrown.
    """
    return is_connection_error(error) or not isinstance(error, ua.UaStatusCodeError)


def _boolean_probe(client, node_id: str, browse_name: str) -> Probe:
    """One ``readBooleanTrue`` capability node, answered three ways."""
    try:
        value = client.get_node(node_id).get_value()
    except Exception as error:
        if _unanswered(error):
            return Probe("unknown", _describe(error, browse_name), error)
        return Probe("not_supported")
    return Probe("supported" if value is True else "not_supported")


def client_supports_history(client) -> Probe:
    """Read history support through an already-connected client."""
    return _boolean_probe(client, HISTORY_NODE_ID, CAPABILITIES["history"]["browseName"])


def client_supports_history_events(client) -> Probe:
    """Read *event* history support through an already-connected client.

    A separate node and a separate answer from :func:`client_supports_history`:
    Part 11 §5.4 lets a server historise values without historising events, and
    most do. Note that the node is commonly absent rather than present-and-false
    — python-opcua's own server has no ns=0;i=11194 at all — which reads the
    same way here, and should: a server that cannot say it keeps event history
    is one whose event history nobody should go looking for.
    """
    return _boolean_probe(
        client, HISTORY_EVENTS_NODE_ID, CAPABILITIES["historyEvents"]["browseName"]
    )


def client_aggregate_functions(client) -> tuple[Probe, dict[str, ua.NodeId]]:
    """The allowlisted aggregate functions the server advertises, through an existing session.

    Browses ``Server/ServerCapabilities/AggregateFunctions`` and keeps only
    children whose browse name *and* node ID match a spec-defined aggregate, so a
    server exposing something unexpected under that folder cannot smuggle in a
    node ID we then send back in a request.
    """
    spec = spec_aggregate_node_ids()
    advertised: dict[str, ua.NodeId] = {}
    try:
        node = client.get_node(AGGREGATE_NODE_ID)
        for child in node.get_referenced_nodes(
            refs=ua.ObjectIds.References,
            direction=ua.BrowseDirection.Forward,
        ):
            try:
                name = child.get_browse_name().Name
            except Exception:
                continue
            if name in spec and child.nodeid == spec[name]:
                advertised[name] = child.nodeid
    except Exception as error:
        if _unanswered(error):
            reason = _describe(error, CAPABILITIES["aggregate"]["browseName"])
            return Probe("unknown", reason, error), {}
        return Probe("not_supported"), {}
    return Probe("supported" if advertised else "not_supported"), advertised


def _describe(error: BaseException, browse_name: str) -> str:
    """A probe failure, as a reason a refusal can quote."""
    return f"reading {browse_name} failed: {str(error) or type(error).__name__}"
