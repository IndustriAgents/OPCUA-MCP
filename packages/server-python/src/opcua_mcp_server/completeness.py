"""Whether a result is the whole answer, as a field (issue #137).

``completeness.ts`` is the Node half. Every tool that can return fewer records
than its request covered puts one of these beside ``result`` in
structuredContent — the shape is ``contract/tools.json`` -> ``completeness``. It
used to be prose: a trailing text block, deliberately kept out of the structured
result, so a client reading ``structuredContent`` could not tell a capped history
read from a complete one, nor a lossy event buffer from a quiet plant.

Free of any OPC UA type, like :mod:`limits`, so both unit suites drive one table
through it: ``tests/fixtures/completeness.json``. The builders below are the only
places either runtime decides what "complete" means for a tool.
"""

from __future__ import annotations

from typing import Any

from .contract import CONTRACT
from .limits import MAX_HISTORY_VALUES


def _assemble(
    *,
    returned: int,
    remaining: bool | None,
    truncated_by: str | None = None,
    limit: int | None = None,
    dropped: int = 0,
    unbrowsable: bool = False,
    continuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the object from its causes, in one fixed order.

    The order of ``reasons`` is truncation first, then loss, then what could not
    be read — fixed so two runtimes reporting the same causes report the same
    array.
    """
    reasons: list[str] = []
    truncated = bool(truncated_by)
    if truncated_by:
        reasons.append(truncated_by)
    if dropped > 0:
        reasons.append("bufferOverflow")
    if unbrowsable:
        reasons.append("unbrowsable")
    return {
        "complete": not reasons,
        "reasons": reasons,
        "returned": returned,
        "truncated": truncated,
        "limit": limit if truncated else None,
        "dropped": dropped,
        "remaining": remaining,
        "continuation": continuation if truncated else None,
    }


def history_completeness(
    *,
    returned: int,
    fetched: int,
    wanted: int | None,
    continuation_point: bool,
    next_start: str | None,
) -> dict[str, Any]:
    """A history read — raw values, stored events, or aggregate intervals.

    ``fetched`` is how many records the OPC UA server sent back, which for event
    history is more than ``returned`` when ``severity_min`` filtered some out: a
    cap is reached by what was fetched, not by what survived the filter.
    ``wanted`` is the count that was asked for, or None for an aggregate read,
    which asks for none.

    Two pieces of evidence, and the first one found decides:

    * the count was reached. The server may well hold more, and whether it does
      is what the continuation point says — a server that returns none leaves the
      answer unknown, so ``remaining`` is None rather than a guess;
    * the server returned a continuation point *short* of the count. That is the
      server's own limit, of a size this server cannot know.

    ``next_start`` is where a forward read resumes: the last record's timestamp,
    inclusive, so the boundary record comes back again rather than being skipped.
    A backward read — the default when no start_time is given — cannot be
    resumed from its arguments, so the caller passes None and ``continuation`` is
    None.
    """
    reached_count = wanted is not None and wanted > 0 and fetched >= wanted
    truncated_by = None
    if reached_count:
        truncated_by = "contractLimit" if wanted >= MAX_HISTORY_VALUES else "requestLimit"
    elif continuation_point:
        truncated_by = "serverLimit"
    if continuation_point:
        remaining: bool | None = True
    elif reached_count:
        remaining = None
    else:
        remaining = False
    return _assemble(
        returned=returned,
        truncated_by=truncated_by,
        limit=wanted if reached_count else None,
        remaining=remaining,
        continuation={"start_time": next_start} if next_start else None,
    )


def drain_completeness(*, returned: int, limit: int, remaining: int, dropped: int) -> dict:
    """``read_events``: a drain of a bounded buffer, which can be short two ways.

    ``remaining`` events still buffered mean ``limit`` stopped the read, and
    calling again unchanged returns the next of them — hence
    ``continuation: {}``. ``dropped`` events are the ones the buffer discarded
    before anyone read them; they are reported, not recoverable.
    """
    more = remaining > 0
    return _assemble(
        returned=returned,
        truncated_by="requestLimit" if more else None,
        limit=limit,
        dropped=dropped,
        remaining=more,
        continuation={},
    )


def traversal_completeness(
    *, returned: int, truncated: bool, max_nodes: int, unbrowsable: bool
) -> dict[str, Any]:
    """``browse_opcua_nodes``: a bounded walk, which can stop early or have gaps.

    ``max_nodes`` is the budget in force after clamping. It is this server's own
    cap when it is ``traversal.maxNodes`` — asking for more is clamped to it —
    and the caller's otherwise. A walk cannot be resumed from its arguments, so
    there is never a ``continuation``: the way to see more is a larger budget or
    a narrower root. ``unbrowsable`` is a node below the root that refused to
    list its children; the walk carries on past it, and whatever was under it is
    missing.
    """
    truncated_by = None
    if truncated:
        truncated_by = (
            "contractLimit" if max_nodes >= CONTRACT["traversal"]["maxNodes"] else "requestLimit"
        )
    return _assemble(
        returned=returned,
        truncated_by=truncated_by,
        limit=max_nodes,
        unbrowsable=unbrowsable,
        remaining=truncated,
    )


def buffer_completeness(records: list[dict]) -> dict[str, Any]:
    """The subscription tools: whole records, each carrying its own ring buffer.

    Nothing truncates the list of subscriptions — there are at most
    ``limits.maxSubscriptions`` of them. What can be lost is what each one
    buffered, and each record says how much in its own ``dropped``; this totals
    them, so a client can test one field for the whole answer.
    """
    return _assemble(
        returned=len(records),
        dropped=sum(record["dropped"] for record in records),
        remaining=False,
    )


__all__ = [
    "buffer_completeness",
    "drain_completeness",
    "history_completeness",
    "traversal_completeness",
]
