"""OPC UA history continuation points: noticing them, and giving them back.

``history.ts`` is the Node half. A server that holds more history than one reply
carries returns a continuation point (Part 11 §6.4.3, Part 4 §5.10.3), and that
is the only evidence a read has that it stopped short of the range — the reason
``completeness.serverLimit`` exists (issue #137). Neither runtime looked at it
before, so a read the server cut short was reported as the whole range: here,
python-opcua's ``Node.read_raw_history`` returns the values and drops the
continuation point on the floor, so the raw read now goes through
``Node.history_read`` with exactly the details ``read_raw_history`` would build.

Nor did either give it back. A continuation point is state the server keeps for
this session until it is released or the session ends, and a server holds only
so many (MaxHistoryContinuationPoints) — so every read left one behind until
later reads started failing with BadNoContinuationPoints. This server never
resumes from one: ``completeness.continuation`` is stateless arguments instead,
which cannot go stale and survives a reconnect. So each is released as soon as
it has been noticed.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from opcua import ua


def continues(continuation_point: bytes | None) -> bool:
    """Whether a history result carries a continuation point."""
    return bool(continuation_point)


def raw_details(start: datetime | None, end: datetime | None, num_values: int) -> Any:
    """The ReadRawModifiedDetails ``Node.read_raw_history`` sends, field for field.

    Built here rather than by that method only because it discards the
    continuation point; the request on the wire is unchanged.
    """
    details = ua.ReadRawModifiedDetails()
    details.IsReadModified = False
    details.StartTime = start or ua.get_win_epoch()
    details.EndTime = end or ua.get_win_epoch()
    details.NumValuesPerNode = num_values
    details.ReturnBounds = True
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


__all__ = ["continues", "raw_details", "release_continuation_point"]
