"""OPC UA events and Alarms & Conditions.

``contract/tools.json`` -> ``events`` is the specification; this module is the
Python implementation of it and ``src/events.ts`` in the Node server is the
other. Both build their EventFilter select clauses from the same field list, in
the same order, and name the resulting record fields the same way — so a client
that has learned one server's events can read the other's.

Why a buffer at all: MCP is request/response, and an OPC UA event arrives when
the server decides. ``subscribe_events`` therefore starts a real subscription and
parks what arrives; ``read_events`` drains it. Nothing is pushed to the client.

python-opcua delivers events on the client's own network thread, so the buffers
here are touched from two threads and take a lock. The library's ``Event`` object
is deliberately *not* used to name the fields: it derives an attribute name from
the first element of a browse path, which collapses ``AckedState`` and
``AckedState/Id`` onto one name and drops the ConditionId entirely. The raw
select clauses and field values it carries alongside are what this reads.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from base64 import b64decode
from collections import deque
from typing import Any

from opcua import ua

from .contract import EVENTS
from .notices import notice
from .records import variant_to_json

#: The Server object — where most servers raise every event they have.
DEFAULT_NOTIFIER: str = EVENTS["defaultNotifierNodeId"]

#: The defaults the contract's tool descriptions promise, shared with Node.
DEFAULTS: dict = EVENTS["defaults"]

#: One event per publishing cycle is not enough. A ConditionRefresh answers with
#: the RefreshStart event, every retained condition and the RefreshEnd event in
#: one go; with python-opcua's default queue size the server discards all but the
#: last, and a refresh looks like it found no alarms at all.
_QUEUE_SIZE = 1000
_PUBLISHING_INTERVAL_MS = 200

#: BaseEventType. Part 4 §7.4.4.5: a browse path in a SimpleAttributeOperand that
#: names BaseEventType is evaluated without regard to the event's own type, which
#: is how one filter can select condition fields from events that have none.
_BASE_EVENT_TYPE = EVENTS["baseEventTypeNodeId"]

#: The one field that is not a browse path: ConditionId is the NodeId attribute of
#: the condition instance itself. See the contract's `events` comment.
_CONDITION_ID = "ConditionId"


def refresh_timed_out_message(timeout_seconds: float, collected: int) -> str:
    """The message both runtimes give when a ConditionRefresh does not finish.

    Said out loud rather than swallowed. The server answers a refresh with a
    RefreshEnd event, and without one there is no way to know whether the
    conditions collected so far are all of them — returning them as if they were
    would let ``list_active_alarms`` quietly under-report retained alarms, which
    in an industrial setting is the one failure this tool must not have.
    """
    return (
        f"ConditionRefresh did not finish within {timeout_seconds}s: the server sent "
        f"{collected} condition(s) but no RefreshEnd, so there may be more. "
        f"Retry with a larger timeout_seconds."
    )


def dropped_events_message(dropped: int, buffer_size: int) -> str:
    """The message both runtimes give when the buffer overflowed before a read.

    Reported to the caller, not only to stderr: an agent that cannot tell a
    complete event stream from one that lost alarms during a burst will read the
    gap as quiet.
    """
    return notice("droppedEvents", dropped=dropped, buffer_size=buffer_size)


def _select_clause(path: str) -> ua.SimpleAttributeOperand:
    """One select clause for a dotted browse path from the contract."""
    operand = ua.SimpleAttributeOperand()
    if path == _CONDITION_ID:
        operand.TypeDefinitionId = ua.NodeId.from_string(EVENTS["conditionTypeNodeId"])
        operand.BrowsePath = []
        operand.AttributeId = ua.AttributeIds.NodeId
        return operand
    operand.TypeDefinitionId = ua.NodeId.from_string(_BASE_EVENT_TYPE)
    operand.BrowsePath = [ua.QualifiedName(name, 0) for name in path.split(".")]
    operand.AttributeId = ua.AttributeIds.Value
    return operand


def event_filter() -> ua.EventFilter:
    """The contract's select clauses, with no where clause.

    Filtering by severity happens on this side rather than in a ContentFilter:
    it keeps the two runtimes' requests byte-for-byte comparable, and a server
    that mishandles a where clause then cannot silently drop events on us.
    """
    event_filter = ua.EventFilter()
    event_filter.SelectClauses = [_select_clause(field["path"]) for field in EVENTS["fields"]]
    return event_filter


def event_record(fields: list) -> dict:
    """The select-clause values of one event, as the canonical record."""
    return {
        field["key"]: variant_to_json(value)
        for field, value in zip(EVENTS["fields"], fields, strict=False)
    }


def _is_refresh_marker(record: dict) -> bool:
    """True for a record the server sent as part of answering a ConditionRefresh."""
    return record.get("event_type") in (
        EVENTS["refreshStartEventTypeNodeId"],
        EVENTS["refreshEndEventTypeNodeId"],
    )


class _BufferingHandler:
    """python-opcua event handler that keeps the last ``size`` matching events."""

    def __init__(self, severity_min: int, size: int):
        self.severity_min = severity_min
        self.records: deque = deque(maxlen=size)
        self.dropped = 0
        self.lock = threading.Lock()

    def event_notification(self, event) -> None:
        record = event_record(event.event_fields)
        # The refresh markers are protocol bookkeeping, not plant events: a
        # ConditionRefresh triggered by `list_active_alarms` would otherwise
        # pepper every open buffer with them.
        if _is_refresh_marker(record):
            return
        severity = record.get("severity")
        if isinstance(severity, (int, float)) and severity < self.severity_min:
            return
        with self.lock:
            if len(self.records) == self.records.maxlen:
                self.dropped += 1
            self.records.append(record)

    def drain(self, limit: int) -> tuple[list[dict], int, int]:
        """Take up to ``limit`` of the oldest events, removing them."""
        with self.lock:
            taken = [self.records.popleft() for _ in range(min(limit, len(self.records)))]
            dropped, self.dropped = self.dropped, 0
            return taken, len(self.records), dropped

    @property
    def size(self) -> int:
        """The buffer's capacity, as configured by ``subscribe_events``."""
        return self.records.maxlen or 0


class EventSubscriptions:
    """The event subscriptions this server holds, keyed by notifier node.

    Also remembers which condition each event_id came from, so
    ``acknowledge_alarm`` can take the event_id the model just saw and nothing
    else: OPC UA needs both the event_id and the condition's NodeId, but only one
    of them is worth asking a model to carry around.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, tuple[Any, _BufferingHandler]] = {}
        self._condition_of_event: dict[str, str] = {}
        self._lock = threading.Lock()

    def remember(self, records: list[dict]) -> None:
        """Remember the condition behind every event we hand out."""
        with self._lock:
            for record in records:
                event_id, condition_id = record.get("event_id"), record.get("condition_id")
                if isinstance(event_id, str) and isinstance(condition_id, str):
                    self._condition_of_event[event_id] = condition_id

    def condition_for(self, event_id: str) -> str | None:
        """The condition an event_id was reported against, if this server saw it."""
        with self._lock:
            return self._condition_of_event.get(event_id)

    def subscribe(self, client, node_id: str, severity_min: int, buffer_size: int) -> bool:
        """Start (or restart) buffering events from ``node_id``.

        Returns whether an existing subscription was replaced. Reported back to
        the caller: re-subscribing silently discards whatever the previous one
        had buffered, and an agent that cannot tell that happened reads the
        missing events as quiet.
        """
        replaced = self.drop(node_id)
        handler = _BufferingHandler(severity_min, buffer_size)
        subscription = client.create_subscription(_PUBLISHING_INTERVAL_MS, handler)
        # Never leave the OPC UA server holding a subscription this process has
        # forgotten about: it would keep publishing until its lifetime expires,
        # and a handful of failed `subscribe_events` calls would eat the
        # server's subscription quota. Same guard as `subscriptions.py` uses.
        try:
            subscription.subscribe_events(
                client.get_node(node_id).nodeid, evfilter=event_filter(), queuesize=_QUEUE_SIZE
            )
        except Exception:
            with contextlib.suppress(Exception):
                subscription.delete()
            raise
        with self._lock:
            self._subscriptions[node_id] = (subscription, handler)
        return replaced

    def drain(self, node_id: str, limit: int) -> tuple[list[dict], int, int, int] | None:
        """Take up to ``limit`` buffered events, or None when not subscribed."""
        with self._lock:
            entry = self._subscriptions.get(node_id)
        if entry is None:
            return None
        records, remaining, dropped = entry[1].drain(limit)
        self.remember(records)
        return records, remaining, dropped, entry[1].size

    def close_all(self) -> None:
        """Tear every event subscription down — the shutdown path.

        Same rule as the data-change subscriptions in ``subscriptions.py``: an
        OPC UA server left holding a subscription this process has forgotten
        keeps publishing into the void until its lifetime expires, so they go
        before the session does.
        """
        with self._lock:
            node_ids = list(self._subscriptions)
        for node_id in node_ids:
            self.drop(node_id)

    def drop(self, node_id: str) -> bool:
        """Tear down the subscription for ``node_id``. True when there was one."""
        with self._lock:
            entry = self._subscriptions.pop(node_id, None)
        if entry is None:
            return False
        try:
            entry[0].delete()
        except Exception as error:  # pragma: no cover - server-side teardown race
            # A subscription the server has already dropped cannot be deleted,
            # and does not need to be.
            # stderr: stdout is the MCP stdio transport.
            print(
                f"Could not delete the event subscription for {node_id}: {error}",
                file=sys.stderr,
            )
        return True


class _RefreshHandler:
    """Collects the conditions a ConditionRefresh replays, and nothing else."""

    def __init__(self) -> None:
        self.conditions: list[dict] = []
        self.started = False
        self.finished = threading.Event()

    def event_notification(self, event) -> None:
        record = event_record(event.event_fields)
        event_type = record.get("event_type")
        if event_type == EVENTS["refreshStartEventTypeNodeId"]:
            # Anything before this belongs to the live event stream, not to the
            # refresh, and would be reported as an alarm it is not.
            self.started = True
            return
        if event_type == EVENTS["refreshEndEventTypeNodeId"]:
            self.finished.set()
            return
        if self.started and record.get("condition_id") is not None:
            self.conditions.append(record)


def list_active_alarms(client, node_id: str, timeout_seconds: float) -> list[dict]:
    """The conditions the server is retaining right now, via ConditionRefresh.

    The server answers a refresh by re-sending every retained condition to one
    subscription, bracketed by a RefreshStart and a RefreshEnd event. So this
    creates a subscription of its own, asks, collects until the RefreshEnd (or
    the timeout), and tears it down again — deliberately independent of whatever
    ``subscribe_events`` may or may not have running.
    """
    handler = _RefreshHandler()
    subscription = client.create_subscription(_PUBLISHING_INTERVAL_MS, handler)
    try:
        subscription.subscribe_events(
            client.get_node(node_id).nodeid, evfilter=event_filter(), queuesize=_QUEUE_SIZE
        )
        condition_type = client.get_node(EVENTS["conditionTypeNodeId"])
        refresh_method = client.get_node(EVENTS["conditionRefreshMethodNodeId"])
        try:
            condition_type.call_method(
                refresh_method,
                ua.Variant(subscription.subscription_id, ua.VariantType.UInt32),
            )
        except Exception as error:
            raise ValueError(
                f"ConditionRefresh failed with status: {error}. "
                "The server may not implement OPC UA Alarms & Conditions."
            ) from error
        if not handler.finished.wait(timeout=max(0.0, timeout_seconds)):
            raise ValueError(refresh_timed_out_message(timeout_seconds, len(handler.conditions)))
        return handler.conditions
    finally:
        # A subscription the server has already dropped cannot be deleted, and
        # does not need to be.
        with contextlib.suppress(Exception):
            subscription.delete()


def acknowledge_alarm(client, condition_id: str, event_id: str, comment: str) -> None:
    """Acknowledge one condition instance.

    The method is called on the condition itself. Part 9 allows a server not to
    expose condition instances in its address space at all, in which case the
    Acknowledge method of AcknowledgeableConditionType is called with the
    condition as the object — so that well-known method id is the fallback here,
    exactly as ``events.ts`` does it.

    Raises whatever python-opcua raises for a non-Good status, which the caller
    turns into the message the model sees.
    """
    condition = client.get_node(condition_id)
    condition.call_method(
        _acknowledge_method(condition, client),
        ua.Variant(b64decode(event_id), ua.VariantType.ByteString),
        ua.Variant(ua.LocalizedText(comment), ua.VariantType.LocalizedText),
    )


def _acknowledge_method(condition, client):
    """The condition's own Acknowledge method, or the type's when it has none."""
    try:
        for child in condition.get_children():
            if child.get_browse_name().Name == "Acknowledge":
                return child
    except Exception as error:
        print(
            f"Could not browse {condition.nodeid.to_string()} for its Acknowledge method: {error}",
            file=sys.stderr,
        )
    return client.get_node(EVENTS["acknowledgeMethodNodeId"])
