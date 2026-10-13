"""History semantics over normalized records; native pages stay inside the port."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from ..access_level import HISTORY_READ, allows
from ..completeness import history_completeness
from ..datetimes import parse_iso_datetime
from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..limits import MAX_HISTORY_VALUES, LimitExceeded, aggregate_intervals, history_values
from ..policy import format_number
from .model import Model


class HistoryPort(Protocol):
    async def raw(
        self, node_id: str, start: datetime | None, end: datetime | None, wanted: int
    ) -> dict: ...
    async def aggregate(
        self, node_id: str, start: datetime, end: datetime, name: str, interval: float
    ) -> list[dict]: ...


def forward_from(start: datetime | None, end: datetime | None, last: str | None) -> str | None:
    if start is None or (end is not None and end <= start):
        return None
    return last if isinstance(last, str) else None


async def history_failed(node_id: str, error: Exception, model: Model | None) -> AdapterFailure:
    """``historyFailed``, with a hint when the node says it keeps no history.

    A node whose AccessLevel lacks HistoryRead is one the server does not
    promise to historize, and "BadHistoryOperationUnsupported" alone sends an
    agent looking for a fault rather than for another way to get the data. Only
    a hint, after the failure and never instead of the request: AccessLevel is
    what a server advertises, and some keep history they do not advertise.
    """
    reason = describe_error(error)
    if model is not None:
        facts = (await model.port.facts([node_id])).get(node_id)
        if (
            facts is not None
            and facts["status"] == "Good"
            and allows(facts["access_level"], HISTORY_READ) is False
        ):
            hint = message("historyNotRecordedHint", node_id=node_id)
            reason = f"{reason} {hint}"
    return AdapterFailure(
        "history", message("historyFailed", node_id=node_id, reason=reason), error
    )


async def read_history(
    port: HistoryPort,
    request: dict,
    offered: list[str],
    now=lambda: datetime.now(timezone.utc),
    model: Model | None = None,
) -> dict:
    """Raw or aggregated history; with ``model``, a failure says when the node keeps none."""
    node_id = request["node_id"]
    aggregate = request.get("aggregate_function")
    if aggregate is None:
        wanted = history_values(request["num_values"])
        try:
            start = parse_iso_datetime(request.get("start"))
            end = parse_iso_datetime(request.get("end"))
            page = await port.raw(node_id, start, end, wanted)
            records = page["records"]
            return {
                "records": records,
                "completeness": history_completeness(
                    returned=len(records),
                    fetched=len(records),
                    wanted=wanted,
                    continuation_point=page["continued"],
                    next_start=forward_from(
                        start, end, records[-1]["timestamp"] if records else None
                    ),
                ),
            }
        except Exception as error:
            raise await history_failed(node_id, error, model) from error
    if request.get("start") is None:
        raise ApplicationRefusal(message("aggregateNeedsStart"))
    if aggregate not in offered:
        raise ApplicationRefusal(
            "Server does not advertise any aggregate functions"
            if not offered
            else f"Invalid aggregate function. Supported: {', '.join(offered)}"
        )
    try:
        start = parse_iso_datetime(request["start"])
        end = parse_iso_datetime(request.get("end")) or now()
        intervals = aggregate_intervals(
            start.timestamp() * 1000, end.timestamp() * 1000, request["processing_interval"]
        )
        if intervals > MAX_HISTORY_VALUES:
            raise ApplicationRefusal(
                message(
                    "tooManyIntervals",
                    tool="read_opcua_history",
                    count=intervals,
                    processing_interval=format_number(request["processing_interval"]),
                    limit=MAX_HISTORY_VALUES,
                )
            )
        records = await port.aggregate(
            node_id, start, end, aggregate, request["processing_interval"]
        )
        return {
            "records": records,
            "completeness": history_completeness(
                returned=len(records),
                fetched=len(records),
                wanted=None,
                continuation_point=False,
                next_start=None,
            ),
        }
    except (ApplicationRefusal, LimitExceeded):
        raise
    except Exception as error:
        raise await history_failed(node_id, error, model) from error
