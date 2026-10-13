"""OPC UA history continuation points: noticing them, and giving them back.

``history.ts`` is the Node half. A server that holds more history than one reply
carries returns a continuation point (Part 11 §6.4.3, Part 4 §5.10.3), and that
is the only evidence a read has that it stopped short of the range — the reason
``completeness.serverLimit`` exists (issue #137). Neither runtime looked at it
before, so a read the server cut short was reported as the whole range: here,
python-opcua's ``Node.read_raw_history`` returns the values and drops the
continuation point on the floor, so the raw read now goes through
``Node.history_read`` with bounds explicitly disabled by the shared contract.

Raw reads release continuation points and offer stateless start-time arguments.
Aggregate reads instead consume their continuation points inside the same call,
keeping the original interval anchor and server configuration. No native point
is exposed to an MCP caller or kept across calls, sessions or reconnects.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from opcua import ua

from .contract import CONTRACT
from .errors import message
from .limits import MAX_HISTORY_VALUES, LimitExceeded
from .records import history_data


def continues(continuation_point: bytes | None) -> bool:
    """Whether a history result carries a continuation point."""
    return bool(continuation_point)


def raw_details(start: datetime | None, end: datetime | None, num_values: int) -> Any:
    """Raw stored readings, without the library's implicit bounding values."""
    details = ua.ReadRawModifiedDetails()
    details.IsReadModified = False
    details.StartTime = start or ua.get_win_epoch()
    details.EndTime = end or ua.get_win_epoch()
    details.NumValuesPerNode = num_values
    details.ReturnBounds = CONTRACT["history"]["rawReturnBounds"]
    return details


def release_continuation_point(
    client: Any, node_id: str, continuation_point: bytes | None, details: Any
) -> None:
    """Release a continuation point, best-effort.

    ``details`` must be of the kind the original read sent — a server reads it to
    know which history the point belongs to. A failure is swallowed: the read has
    already succeeded, and the worst a lost release costs is the point staying
    held until the session closes, which is what happened to every one before.
    """
    if not continues(continuation_point):
        return
    value_id = ua.HistoryReadValueId()
    value_id.NodeId = client.get_node(node_id).nodeid
    value_id.IndexRange = ""
    value_id.ContinuationPoint = continuation_point

    params = ua.HistoryReadParameters()
    params.HistoryReadDetails = details
    params.TimestampsToReturn = ua.TimestampsToReturn.Both
    params.ReleaseContinuationPoints = True
    params.NodesToRead.append(value_id)
    # Best-effort by design; see the docstring.
    with contextlib.suppress(Exception):
        client.uaclient.history_read(params)


def read_continuation(client: Any, node_id: str, continuation_point: bytes, details: Any) -> Any:
    """Resume on the same session using the original processed-history details."""
    value_id = ua.HistoryReadValueId()
    value_id.NodeId = client.get_node(node_id).nodeid
    value_id.IndexRange = ""
    value_id.ContinuationPoint = continuation_point
    params = ua.HistoryReadParameters()
    params.HistoryReadDetails = details
    params.TimestampsToReturn = ua.TimestampsToReturn.Both
    params.ReleaseContinuationPoints = False
    params.NodesToRead = [value_id]
    results = client.uaclient.history_read(params)
    if len(results) != 1:
        raise ValueError("Read aggregate failed: expected one history result")
    return results[0]


def aggregate_pages(
    first: Any,
    read_next: Callable[[bytes], Any],
    release: Callable[[bytes], None],
    max_values: int = MAX_HISTORY_VALUES,
) -> list:
    """Drain a bounded aggregate query; never return an unfinished range.

    A continuation with no values is refused rather than spun on. Every
    continuation request must add a value, so at most max_values pages are
    read. On failure/cancellation the currently held point is released.
    """
    held = getattr(first, "ContinuationPoint", None)
    result = first
    values: list = []
    try:
        while True:
            point = getattr(result, "ContinuationPoint", None)
            if point:
                held = point
            page = history_data(result, "Read aggregate", "DataValues")
            if len(values) + len(page) > max_values:
                raise LimitExceeded(message("aggregatePageLimit", limit=max_values))
            values.extend(page)
            if not point:
                held = None
                return values
            if not page:
                raise LimitExceeded(message("aggregateNoProgress"))
            if len(values) == max_values:
                raise LimitExceeded(message("aggregatePageLimit", limit=max_values))
            result = read_next(point)
    finally:
        if held:
            with contextlib.suppress(Exception):
                release(held)


__all__ = [
    "aggregate_pages",
    "continues",
    "raw_details",
    "read_continuation",
    "release_continuation_point",
]
