"""OPC UA data-change subscriptions and their monitored items.

An MCP tool call is request/response, so a subscription cannot answer the caller
directly: the notifications arrive whenever the OPC UA server decides to publish,
long after ``subscribe_opcua_node`` has returned. What this module does instead is
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

from .records import history_record

#: Defaults and bounds, shared verbatim with the Node server. They appear in the
#: record the agent reads back, so a difference between the runtimes is drift.
DEFAULT_PUBLISHING_INTERVAL = 1000
MIN_PUBLISHING_INTERVAL = 50
DEFAULT_BUFFER_SIZE = 20
MIN_BUFFER_SIZE = 1
MAX_BUFFER_SIZE = 1000


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
    return f"No such subscription: {subscription_id}"


@dataclass
class _Entry:
    """One live subscription, its monitored item, and what it has delivered."""

    id: str
    node_id: str
    publishing_interval: float
    sampling_interval: float
    buffer_size: int
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
                "changes": list(self.changes),
            }


class _DataChangeHandler:
    """python-opcua's handler protocol, forwarding into one entry's buffer.

    One handler per subscription, because this server creates one subscription
    per monitored node — which is what lets a single ``unsubscribe_opcua_node``
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
        """Bind the manager to the connected OPC UA client, at lifespan start."""
        self._client = client

    def subscribe(
        self,
        node_id: str,
        publishing_interval: float | None = None,
        sampling_interval: float | None = None,
        buffer_size: int | None = None,
    ) -> dict:
        """Start monitoring ``node_id``, returning the new subscription's record."""
        if self._client is None:
            raise ValueError("No OPC UA session available")

        publishing, sampling, size = resolve_options(
            publishing_interval, sampling_interval, buffer_size
        )
        node = self._client.get_node(node_id)

        with self._lock:
            self._counter += 1
            entry_id = f"sub-{self._counter}"

        entry = _Entry(
            id=entry_id,
            node_id=node_id,
            publishing_interval=publishing,
            sampling_interval=sampling,
            buffer_size=size,
            changes=deque(maxlen=size),
        )
        # The handler is built before the subscription so it can be passed in
        # rather than patched on afterwards, and the subscription before the
        # monitored item: the OPC UA server sends the node's current value the
        # moment the item exists, and that first notification is a change the
        # agent should see.
        subscription = self._client.create_subscription(publishing, _DataChangeHandler(entry))
        entry.subscription = subscription

        try:
            handle = subscription.subscribe_data_change(node, queuesize=size)
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

        with self._lock:
            self._entries[entry_id] = entry
        return entry.as_record()

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
        _delete_quietly(entry.subscription)
        return record

    def close_all(self) -> None:
        """Tear every subscription down — the shutdown path."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            _delete_quietly(entry.subscription)


def _delete_quietly(subscription: Any) -> None:
    """Delete without letting a dead session's error escape.

    Every caller is a cleanup path: an OPC UA server that has already dropped the
    subscription (or the whole session) is the normal case on shutdown, and
    failing there would turn a tidy exit into a crash.
    """
    try:
        subscription.delete()
    except Exception as error:
        print(f"Error deleting OPC UA subscription: {error}", file=sys.stderr)


#: The one manager the tools and the `opcua://subscriptions` resource share.
SUBSCRIPTIONS = SubscriptionManager()
