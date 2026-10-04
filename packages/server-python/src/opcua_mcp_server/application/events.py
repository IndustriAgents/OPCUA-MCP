"""Event subscription, drain and history semantics over a per-invocation port."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..completeness import drain_completeness, history_completeness
from ..datetimes import parse_iso_datetime
from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..limits import event_buffer_size, history_values
from ..node_ids import canonical_node_id
from ..notices import notice
from .history import forward_from


class EventPort(Protocol):
    async def subscribe(self, node_id: str, severity: int, size: int) -> bool: ...
    async def drain(self, node_id: str, limit: int) -> dict | None: ...
    async def history(
        self, node_id: str, start: datetime, end: datetime, wanted: int, severity: int
    ) -> dict: ...


async def subscribe_events(port: EventPort, node_id: str, severity: int, requested: int) -> dict:
    size = event_buffer_size(requested)
    try:
        replaced = await port.subscribe(node_id, severity, size)
    except Exception as error:
        raise AdapterFailure(
            "event-subscribe",
            message("eventSubscribeFailed", node_id=node_id, reason=describe_error(error)),
            error,
        ) from error
    return {
        "node_id": canonical_node_id(node_id),
        "severity_min": severity,
        "buffer_size": size,
        "replaced": replaced,
    }


async def read_events(port: EventPort, node_id: str, limit: int) -> dict:
    drained = await port.drain(node_id, limit)
    if drained is None:
        raise ApplicationRefusal(message("notSubscribedToEvents", node_id=node_id))
    notices = []
    if drained["dropped"] > 0:
        notices.append(
            notice("droppedEvents", dropped=drained["dropped"], buffer_size=drained["size"])
        )
    if drained["resubscribed"]:
        notices.append(notice("eventsResubscribed"))
    return {
        "records": drained["records"],
        "completeness": drain_completeness(
            returned=len(drained["records"]),
            limit=limit,
            remaining=drained["remaining"],
            dropped=drained["dropped"],
        ),
        "notices": notices,
    }


async def read_event_history(
    port: EventPort, request: dict, now=lambda: datetime.now(timezone.utc)
) -> dict:
    try:
        end = parse_iso_datetime(request.get("end")) or now()
        start = parse_iso_datetime(request.get("start")) or end - timedelta(hours=1)
    except ValueError as error:
        raise ApplicationRefusal(str(error)) from error
    wanted = history_values(request["num_values"])
    try:
        page = await port.history(request["node_id"], start, end, wanted, request["severity_min"])
    except Exception as error:
        raise AdapterFailure(
            "event-history",
            message("eventHistoryFailed", node_id=request["node_id"], reason=describe_error(error)),
            error,
        ) from error
    return {
        "records": page["records"],
        "completeness": history_completeness(
            returned=len(page["records"]),
            fetched=page["fetched"],
            wanted=wanted,
            continuation_point=page["continued"],
            next_start=forward_from(start, end, page["last_time"]),
        ),
    }
