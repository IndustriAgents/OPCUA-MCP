"""OPC UA data-change subscriptions and their monitored items.

An MCP tool call is request/response, so a subscription cannot answer the caller
directly: the notifications arrive whenever the OPC UA server decides to publish,
long after ``subscribe_opcua_nodes`` has returned. What this module does instead is
own the OPC UA subscription and *buffer* what it delivers, so the agent can read
the accumulated changes back at its own pace — through ``list_subscriptions`` or
the ``opcua://subscriptions`` resource.

``src/subscriptions.ts`` in the Node server is the other implementation of the
same contract (``resultShapes.subscriptionRecords``), and the two must produce the
same record for the same subscription.

Thread safety is not optional here. python-opcua delivers data changes on its own
publishing thread, while the MCP tools read the buffer from the asyncio loop, so
every touch of a subscription's counters goes through a lock.
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from opcua import ua

from .contract import CONTRACT
from .errors import message
from .records import history_record

# Defaults and bounds, read from the contract rather than written here. They were
# five constants declared identically in this file and in `subscriptions.ts` —
# two copies of the same promise, which the tool descriptions also quote, so a
# change had to be made in three places to be true. Now it is made in one.
_LIMITS = CONTRACT["subscriptions"]
DEFAULT_PUBLISHING_INTERVAL = _LIMITS["defaultPublishingIntervalMs"]
MIN_PUBLISHING_INTERVAL = _LIMITS["minPublishingIntervalMs"]
DEFAULT_BUFFER_SIZE = _LIMITS["defaultBufferSize"]
MIN_BUFFER_SIZE = _LIMITS["minBufferSize"]
MAX_BUFFER_SIZE = _LIMITS["maxBufferSize"]

#: The deadband kinds the contract names, mapped onto python-opcua's enum. The
#: names are the contract's, the numbers are the library's, and the numbering is
#: fixed by OPC UA Part 4 §7.22 — so the Node half maps the same names onto its
#: own library and the two provably agree without either transcribing a number.
DEADBAND_TYPES: dict[str, int] = {
    "none": ua.DeadbandType.None_,
    "absolute": ua.DeadbandType.Absolute,
    "percent": ua.DeadbandType.Percent,
}

#: The same, for what counts as a change worth reporting.
DATA_CHANGE_TRIGGERS: dict[str, int] = {
    "status": ua.DataChangeTrigger.Status,
    "statusValue": ua.DataChangeTrigger.StatusValue,
    "statusValueTimestamp": ua.DataChangeTrigger.StatusValueTimestamp,
}

#: Not OPC UA's default, which is `Status`. An agent that asked to watch a value
#: and was told only about status transitions would have been given something
#: nobody asks for.
DEFAULT_DATA_CHANGE_TRIGGER = _LIMITS["defaultDataChangeTrigger"]


@dataclass(frozen=True)
class Filter:
    """What a subscription reports, beyond how often it looks.

    Point a subscription at a noisy analogue tag with no deadband and the default
    20-record ring fills with sensor jitter in about a second: the agent reads it
    back, sees nothing but noise, and has spent one of the server's subscriptions
    to get it. This is OPC UA's own answer (Part 4 §7.22) rather than filtering
    after the fact — the values never leave the server, so it costs no bandwidth
    and no buffer.
    """

    deadband_type: str = "none"
    deadband_value: float = 0.0
    trigger: str = DEFAULT_DATA_CHANGE_TRIGGER

    @property
    def is_default(self) -> bool:
        """True when this asks for nothing the server would not do anyway."""
        return self.deadband_type == "none" and self.trigger == DEFAULT_DATA_CHANGE_TRIGGER


def resolve_options(
    publishing_interval: float | None,
    sampling_interval: float | None,
    buffer_size: int | None,
) -> tuple[float, float, int]:
    """The intervals and buffer size a subscribe request resolves to.

    Free of any OPC UA type so both unit suites can pin the same defaults.
    """
    publishing = max(
        _number(publishing_interval, DEFAULT_PUBLISHING_INTERVAL), MIN_PUBLISHING_INTERVAL
    )
    # 0 means "sample as often as you publish" — resolved here rather than passed
    # through, so the record reports the interval that is actually in force.
    requested_sampling = _number(sampling_interval, 0)
    sampling = requested_sampling if requested_sampling > 0 else publishing
    size = min(
        max(int(_number(buffer_size, DEFAULT_BUFFER_SIZE)), MIN_BUFFER_SIZE), MAX_BUFFER_SIZE
    )
    return publishing, sampling, size


def resolve_filter(
    deadband_type: str | None,
    deadband_value: float | None,
    data_change_trigger: str | None,
) -> Filter:
    """The filter a subscribe request resolves to, or raise if it cannot.

    Validation the contract's own schema cannot express: the `enum` keyword
    refuses an unknown name, but "a deadband needs a size" is a relationship
    *between* two arguments. Refused rather than defaulted to zero, which would
    be a deadband that filters nothing while reporting that one is in force —
    the caller would read a buffer full of jitter and conclude the tag was
    noisier than their threshold, which it may not be.
    """
    kind = deadband_type or "none"
    if kind not in DEADBAND_TYPES:
        raise ValueError(
            message(
                "notAllowedValue",
                tool="subscribe_opcua_nodes",
                argument="deadband_type",
                allowed=", ".join(f'"{name}"' for name in DEADBAND_TYPES),
                value=f'"{kind}"',
            )
        )
    trigger = data_change_trigger or DEFAULT_DATA_CHANGE_TRIGGER
    if trigger not in DATA_CHANGE_TRIGGERS:
        raise ValueError(
            message(
                "notAllowedValue",
                tool="subscribe_opcua_nodes",
                argument="data_change_trigger",
                allowed=", ".join(f'"{name}"' for name in DATA_CHANGE_TRIGGERS),
                value=f'"{trigger}"',
            )
        )
    if kind == "none":
        return Filter(deadband_type="none", deadband_value=0.0, trigger=trigger)
    if deadband_value is None:
        raise ValueError(message("deadbandNeedsValue", deadband_type=kind))
    return Filter(
        deadband_type=kind,
        deadband_value=_number(deadband_value, 0.0),
        trigger=trigger,
    )


def _number(value: Any, fallback: float) -> float:
    """``value`` as a float, or ``fallback`` when it is absent or not a number."""
    if value is None or isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if number != number else number  # NaN is never equal to itself


def unknown_subscription_message(subscription_id: str) -> str:
    """The message both runtimes give for an ID that is not (or no longer) active."""
    return message("unknownSubscription", subscription_id=subscription_id)


def unknown_subscriptions_message(subscription_ids: list[str]) -> str:
    """The same, for a batch cancel — named in full so the caller sees which failed.

    Every id is checked before any subscription is cancelled, so this message
    means nothing was cancelled: a partial cancel would leave the caller unable
    to tell which handles still work, and would lose the buffered changes of the
    ones that did go, to a typo.
    """
    if len(subscription_ids) == 1:
        return unknown_subscription_message(subscription_ids[0])
    return message("unknownSubscriptions", subscription_ids=", ".join(subscription_ids))


def delete_failed_message(subscription_id: str, reason: str) -> str:
    """The message both runtimes give when the server refuses an explicit cancel.

    Worth saying out loud rather than swallowing: the subscription is gone from
    this process either way, so the ID cannot be retried, but the OPC UA server
    may still be publishing into the void. Only the explicit path reports this —
    on shutdown a refused delete is the normal case, not news.
    """
    return message("subscriptionDeleteFailed", subscription_id=subscription_id, reason=reason)


@dataclass
class _Entry:
    """One live subscription, its monitored item, and what it has delivered."""

    id: str
    node_id: str
    publishing_interval: float
    sampling_interval: float
    buffer_size: int
    data_filter: Filter = field(default_factory=Filter)
    subscription: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    change_count: int = 0
    changes: deque = field(default_factory=deque)

    def record_change(self, data_value: Any) -> None:
        """Buffer one data change. Called on python-opcua's publishing thread."""
        with self.lock:
            self.change_count += 1
            self.changes.append(history_record(data_value))

    def as_record(self) -> dict:
        with self.lock:
            return {
                "subscription_id": self.id,
                "node_id": self.node_id,
                "publishing_interval": self.publishing_interval,
                "sampling_interval": self.sampling_interval,
                "buffer_size": self.buffer_size,
                "change_count": self.change_count,
                # The filter in force, reported for the same reason the intervals
                # are: a caller reading a suspiciously quiet buffer needs to know
                # whether it asked for that.
                "deadband_type": self.data_filter.deadband_type,
                "deadband_value": self.data_filter.deadband_value,
                "data_change_trigger": self.data_filter.trigger,
                "changes": list(self.changes),
            }


def _monitoring_filter(data_filter: Filter) -> Any:
    """One ``DataChangeFilter``, or None when the defaults are what is wanted.

    None rather than a filter that asks for the defaults: a server is entitled to
    reject a filter it does not implement, and there is no reason to risk that for
    a subscription that wanted nothing special. This is also why the trigger is
    only sent when it differs from what this server treats as its default.
    """
    if data_filter.is_default:
        return None
    request = ua.DataChangeFilter()
    request.Trigger = DATA_CHANGE_TRIGGERS[data_filter.trigger]
    request.DeadbandType = DEADBAND_TYPES[data_filter.deadband_type]
    request.DeadbandValue = data_filter.deadband_value
    return request


class _DataChangeHandler:
    """python-opcua's handler protocol, forwarding into one entry's buffer.

    One handler per subscription, because this server creates one subscription
    per monitored node — which is what lets a single ``unsubscribe_opcua_nodes``
    take the whole thing down rather than leaving an empty subscription behind.
    """

    def __init__(self, entry: _Entry) -> None:
        self._entry = entry

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        # `data.monitored_item.Value` is the full DataValue — value, source
        # timestamp and status — where `val` is only the bare value. The record
        # shape needs all three.
        self._entry.record_change(data.monitored_item.Value)


class SubscriptionManager:
    """Every OPC UA subscription this MCP server has created, and their buffers.

    Module-level rather than per-request state: an MCPServer *resource* handler
    is given no ``Context`` for a static URI, so ``opcua://subscriptions`` cannot
    reach the lifespan context and has to read the manager directly. A stdio
    server serves exactly one client, so the single instance is the whole world.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._client: Any = None

    def attach(self, client: Any) -> None:
        """Bind the manager to the connected OPC UA client.

        Called whenever the connection produces a client — at startup and again
        after every reconnect, since a reconnect replaces the object.
        """
        self._client = client

    def subscribe(
        self,
        node_id: str,
        publishing_interval: float | None = None,
        sampling_interval: float | None = None,
        buffer_size: int | None = None,
        data_filter: Filter | None = None,
    ) -> dict:
        """Start monitoring ``node_id``, returning the new subscription's record."""
        if self._client is None:
            raise ValueError("No OPC UA session available")

        publishing, sampling, size = resolve_options(
            publishing_interval, sampling_interval, buffer_size
        )

        with self._lock:
            self._counter += 1
            entry_id = f"sub-{self._counter}"

        entry = _Entry(
            id=entry_id,
            node_id=node_id,
            publishing_interval=publishing,
            sampling_interval=sampling,
            buffer_size=size,
            data_filter=data_filter or Filter(),
            changes=deque(maxlen=size),
        )
        self._attach(self._client, entry)

        with self._lock:
            self._entries[entry_id] = entry
        return entry.as_record()

    def reattach(self, client: Any) -> None:
        """Re-create every subscription on ``client``, after the old one died.

        What makes an OPC UA MCP server survivable across a plant restart: the
        subscription IDs the agent is holding keep working, and the changes
        already buffered are still there to be read — only the gap while the
        server was away is missing, which no amount of client-side effort could
        have filled.

        Best-effort per subscription. One the server will not take back (its node
        is gone from the new address space, say) is dropped rather than left in
        the list as a handle that will never deliver again: ``list_subscriptions``
        has to keep telling the truth.
        """
        self.attach(client)
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            try:
                self._attach(client, entry)
                print(
                    f"Re-established subscription {entry.id} on node {entry.node_id}",
                    file=sys.stderr,
                )
            except Exception as error:
                with self._lock:
                    self._entries.pop(entry.id, None)
                print(
                    f"Could not re-establish subscription {entry.id} on node "
                    f"{entry.node_id}: {error}",
                    file=sys.stderr,
                )

    def _attach(self, client: Any, entry: _Entry) -> None:
        """Create the OPC UA subscription and monitored item behind one entry.

        Shared by the first subscribe and by every re-establishment after a
        reconnect, so the two cannot drift into asking the server for different
        things — the record the agent reads back names intervals that would
        otherwise silently stop being the ones in force.
        """
        node = client.get_node(entry.node_id)
        publishing, sampling, size = (
            entry.publishing_interval,
            entry.sampling_interval,
            entry.buffer_size,
        )
        # The handler is built before the subscription so it can be passed in
        # rather than patched on afterwards, and the subscription before the
        # monitored item: the OPC UA server sends the node's current value the
        # moment the item exists, and that first notification is a change the
        # agent should see.
        subscription = client.create_subscription(publishing, _DataChangeHandler(entry))

        try:
            monitoring_filter = _monitoring_filter(entry.data_filter)
            if monitoring_filter is None:
                handle = subscription.subscribe_data_change(node, queuesize=size)
            else:
                # `_subscribe` is `subscribe_data_change` with the one parameter
                # that wrapper does not pass on. python-opcua offers no public way
                # to attach a general DataChangeFilter when the item is created —
                # `deadband_monitor` hardcodes the trigger, and
                # `modify_monitored_item` can only express an absolute deadband —
                # and attaching one afterwards would leave a window in which the
                # unfiltered item is already delivering. The reach is confined to
                # this branch, and `test_subscriptions.py` asserts the method
                # still exists on the real class, so a library change fails loudly
                # instead of silently dropping the filter.
                handle = subscription._subscribe(
                    node,
                    ua.AttributeIds.Value,
                    mfilter=monitoring_filter,
                    queuesize=size,
                )
            if sampling != publishing:
                # python-opcua hardcodes a monitored item's SamplingInterval to
                # the subscription's publishing interval, so an independent
                # sampling rate has to be asked for as a modification.
                subscription.modify_monitored_item(handle, sampling, size)
        except Exception:
            # Never leave the OPC UA server holding a subscription this process
            # has forgotten about: it would keep publishing until its lifetime
            # expired.
            _delete_quietly(subscription)
            raise
        entry.subscription = subscription

    def list(self) -> list[dict]:
        """Every active subscription, in the order it was created."""
        with self._lock:
            entries = list(self._entries.values())
        return [entry.as_record() for entry in entries]

    def unsubscribe(self, subscription_id: str) -> dict:
        """Cancel one subscription, returning its final record.

        Raises ``KeyError`` when the ID is not active.
        """
        with self._lock:
            # Drop it from the map first: even if delete() fails, the agent must
            # not be told a subscription is still active when nothing is
            # listening to it.
            entry = self._entries.pop(subscription_id, None)
        if entry is None:
            raise KeyError(subscription_id)
        record = entry.as_record()
        if entry.subscription is None:
            return record
        try:
            entry.subscription.delete()
        except Exception as error:
            # Unlike shutdown, an explicit cancel reports this. The caller asked
            # for something specific and did not fully get it, and no longer
            # holds an ID to retry with.
            raise RuntimeError(delete_failed_message(subscription_id, str(error))) from error
        return record

    def close_all(self) -> None:
        """Tear every subscription down — the shutdown path."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.subscription is not None:
                _delete_quietly(entry.subscription)


def _delete_quietly(subscription: Any) -> None:
    """Delete without letting a dead session's error escape.

    The remaining callers are cleanup paths: an OPC UA server that has already
    dropped the subscription (or the whole session) is the normal case on
    shutdown, and failing there would turn a tidy exit into a crash. An explicit
    ``unsubscribe`` does *not* go through here — see `delete_failed_message`.
    """
    try:
        subscription.delete()
    except Exception as error:
        print(f"Error deleting OPC UA subscription: {error}", file=sys.stderr)
