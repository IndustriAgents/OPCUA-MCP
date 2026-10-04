"""Alarm result/framing semantics; the port performs one native action per invocation."""

from __future__ import annotations

from typing import Protocol

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id


class AlarmPort(Protocol):
    async def list(self, node_id: str, timeout: float) -> list[dict]: ...
    def remember(self, records: list[dict]) -> None: ...
    def condition_for(self, event_id: str) -> str | None: ...
    async def action(
        self, condition_id: str, event_id: str, action: str, comment: str, duration: float | None
    ) -> dict: ...


async def list_alarms(port: AlarmPort, node_id: str, timeout: float) -> list[dict]:
    try:
        records = await port.list(node_id, timeout)
    except Exception as error:
        raise AdapterFailure(
            "alarms", message("alarmsFailed", node_id=node_id, reason=describe_error(error)), error
        ) from error
    port.remember(records)
    return records


async def act_on_alarm(
    port: AlarmPort,
    event_id: str,
    action: str,
    comment: str,
    duration: float | None,
    condition_id: str | None,
    acknowledgement: bool,
) -> dict:
    if action == "shelveFor" and duration is None:
        raise ApplicationRefusal(message("shelveForNeedsDuration"))
    if action != "shelveFor" and duration is not None:
        raise ApplicationRefusal(message("shelveDurationNotAllowed", action=action))
    condition = condition_id or port.condition_for(event_id)
    if not condition:
        raise ApplicationRefusal(message("unknownEventId", event_id=event_id))

    def failed(reason):
        return (
            message("acknowledgeFailed", condition_id=condition, reason=reason)
            if acknowledgement
            else message("alarmActionFailed", action=action, condition_id=condition, reason=reason)
        )

    try:
        status = await port.action(condition, event_id, action, comment, duration)
    except Exception as error:
        raise AdapterFailure("alarm-action", failed(describe_error(error)), error) from error
    if not status["good"]:
        error = ValueError(status["status"])
        raise AdapterFailure("alarm-action", failed(status["status"]), error) from error
    record = {
        "event_id": event_id,
        "condition_id": canonical_node_id(condition),
        "status": status["status"],
    }
    if not acknowledgement:
        record["action"] = action
    return record
