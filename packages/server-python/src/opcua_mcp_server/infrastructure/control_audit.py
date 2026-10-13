"""Durable control-audit adapter; targets and session facts contain no written values."""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timezone
from typing import Any

from ..audit import AuditWriteError, build_record, operator_id
from ..contract import CONTRACT
from ..errors import ApplicationRefusal
from ..errors import message as error_message
from ..policy import control_gate, values_at
from ..state import ServerState


def _audit_targets(spec: dict, arguments: dict[str, Any]) -> dict[str, Any]:
    """What a control call was aimed at, for the audit record."""
    guard = spec.get("guard")
    if not guard:
        return {}
    record: dict[str, Any] = {}

    node_ids = [
        value for path in guard.get("nodeIdPaths", []) for value in values_at(arguments, path)
    ]
    if node_ids:
        record["node_ids"] = node_ids

    for pair in guard.get("methodPaths", []):
        objects = values_at(arguments, pair["objectPath"])
        methods = values_at(arguments, pair["methodPath"])
        record["object_node_id"] = objects[0] if objects else None
        record["method_node_id"] = methods[0] if methods else None
        break

    for path in guard.get("auditPaths", []):
        # Only what is present: an absent optional argument is not a target, and
        # recording it as null would make every acknowledgement look
        # half-specified.
        values = values_at(arguments, path)
        if values:
            record[path] = values[0]
    return record


def new_call_id() -> str:
    """An id for one tool call, to tie its audit lines together."""
    return secrets.token_hex(8)


def describe_targets(spec: dict, arguments: dict[str, Any]) -> str:
    """What a call was aimed at, for a message a human will read."""
    targets = _audit_targets(spec, arguments)
    if not targets:
        return "unknown"
    parts = []
    for key, value in targets.items():
        rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else value
        parts.append(f"{key}={rendered}")
    return "; ".join(parts)


def _audit_decision(
    state: ServerState,
    name: str,
    arguments: dict[str, Any],
    decision: str,
    reason: str = "",
    call_id: str | None = None,
    attempt: int = 1,
) -> None:
    """Write one line of the control audit trail."""
    spec = next((tool for tool in CONTRACT["tools"] if tool["name"] == name), None)
    if spec is None or spec["accessClass"] not in {"control", "alarm-action"}:
        return
    connection = state.connection
    record = build_record(
        # Milliseconds, as `Date.toISOString` writes them, so the two runtimes'
        # timestamps are the same shape and not merely the same instant.
        timestamp=datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        call_id=call_id,
        attempt=attempt,
        endpoint=state.url,
        session=state.session_id,
        session_generation=connection.session_generation if connection is not None else None,
        # null when OPCUA_OPERATOR_ID is unset, which is honest: this server has
        # no notion of who is calling, and a name nothing verified would be worse
        # than none.
        operator_label=operator_id(),
        **state.audit.identity(),
        profile=state.policy.config.profile,
        # What let control through, or kept it out: `secured` for a verified
        # server, or which lab override was in force. An override that shows
        # up only in a startup line nobody kept is an override nobody can audit.
        control=control_gate(state.policy.config),
        server_authentication_method=(
            "trust-store"
            if getattr(
                getattr(state.policy.config, "server_identity", None), "authentication_method", None
            )
            == "trust-store"
            else None
        ),
        tool=name,
        decision=decision,
        targets=_audit_targets(spec, arguments),
        reason=reason or None,
    )
    state.audit.write(record)


def _audit_after(state: ServerState, name: str, arguments: dict[str, Any], *args, **kwargs):
    """Record a denial or an outcome, reporting rather than raising if it is lost."""
    try:
        _audit_decision(state, name, arguments, *args, **kwargs)
    except AuditWriteError as error:
        print(
            f"AUDIT FAILURE: the {args[0]} record for {name} (call {kwargs.get('call_id')}) "
            f"was not written: {error}",
            file=sys.stderr,
        )


def _audit_permission(
    state: ServerState, name: str, arguments: dict[str, Any], *, call_id: str, attempt: int
) -> None:
    """Record ``allowed`` before anything is sent — or refuse the call."""
    try:
        _audit_decision(state, name, arguments, "allowed", call_id=call_id, attempt=attempt)
    except AuditWriteError as error:
        print(f"AUDIT FAILURE: refusing {name} (call {call_id}): {error}", file=sys.stderr)
        raise ApplicationRefusal(
            error_message("auditUnavailable", tool=name, reason=str(error))
        ) from error
