"""What the information model and the model-aware policy add to a tool call.

A use case takes an optional :class:`Model`. With one, the call is also checked
against what the nodes themselves publish (``node_facts``) and against the
policy rules that need a reading to decide — interlocks and the alarm scope.
Without one it is the orchestration the port tables in
``tests/fixtures/*-port.json`` pin. (A write is planned from the facts either
way, through its own port; the model adds only its interlocks.) The rules are
the pure functions in ``write_plan.py`` and ``policy_check.py``; this module
only gathers their inputs through a port, so nothing here touches a client
library.

The plant model only ever narrows: a fact that could not be read skips its check
(the server enforces its own rights), while a policy rule that cannot be applied
refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import ApplicationRefusal, message
from ..policy import ToolPolicy, ValueBound
from ..policy_check import check_alarm_scope, check_preconditions


class ModelPort(Protocol):
    async def facts(self, node_ids: list[str]) -> dict[str, dict | None]: ...
    async def on_object(self, object_node_id: str, method_node_id: str) -> bool | None: ...
    async def readings(self, node_ids: list[str]) -> list[dict]: ...
    async def condition(self, condition_id: str) -> dict[str, tuple[str, Any]]: ...


def bound_json(bound: ValueBound | None) -> dict[str, Any] | None:
    """A value bound as the shared tables spell one."""
    if bound is None:
        return None
    return {
        "min": bound.minimum,
        "max": bound.maximum,
        "enum": list(bound.allowed) if bound.allowed is not None else None,
        "max_change": bound.max_change,
    }


@dataclass(frozen=True)
class Model:
    """The model port of one call, and the policy whose model-aware rules it applies."""

    port: ModelPort
    policy: ToolPolicy

    def write_policy(self, node_id: str) -> dict[str, Any]:
        """``write_access``'s policy half for one node (see write-access.json)."""
        policy = self.policy
        operator = policy.config.profile == "operator"
        return {
            "tool_refusal": policy.write_refusal(),
            "operator": operator,
            "allowlisted": operator and policy.is_allowlisted(node_id),
            "bound": bound_json(policy.bound_for(node_id)) if operator else None,
            "allow_out_of_range": policy.config.allow_out_of_range_writes,
        }

    # --- interlocks ----------------------------------------------------------------

    def require_resolved_interlocks(self) -> None:
        """Refuse all control while an interlock's target names no node on this session.

        Whatever this call is aimed at: the interlock that cannot be tied to its
        node may be the one meant for this call's target, and there is no way to
        tell.
        """
        entry = self.policy.unresolved_precondition()
        if entry is not None:
            raise ApplicationRefusal(message("preconditionUnresolved", entry=entry))

    async def _current(self, entries: list[str]) -> dict[str, dict | None]:
        """Each policy entry's current Value, as ``check_preconditions`` takes it.

        Keyed by the entry as written. None for one that resolves to no node; a
        Bad status for one the server would not read. One Read for all of them,
        right before the guarded request goes out — an interlock read any
        earlier is one that may have changed since.
        """
        wanted = list(dict.fromkeys(entries))
        resolved = {entry: self.policy.resolve(entry) for entry in wanted}
        readable = [entry for entry in wanted if resolved[entry] is not None]
        readings = await self.port.readings([resolved[entry] for entry in readable])
        current: dict[str, dict | None] = dict.fromkeys(wanted)
        current.update(zip(readable, readings, strict=True))
        return current

    async def check_write_interlocks(self, node_ids: list[str]) -> None:
        """Refuse the whole batch if an interlock on any node about to be written does not hold."""
        self.require_resolved_interlocks()
        guarded = self.policy.preconditions_for_write(node_ids)
        if not guarded:
            return
        current = await self._current(
            [requirement["node"] for _, interlocks in guarded for requirement in interlocks]
        )
        for node_id, interlocks in guarded:
            refusal = check_preconditions(
                {"target": node_id, "requirements": interlocks, "current": current}
            )
            if refusal is not None:
                raise ApplicationRefusal(refusal)

    async def check_call_interlocks(self, object_node_id: str, method_node_id: str) -> None:
        """Refuse a method call whose interlocks do not hold now."""
        self.require_resolved_interlocks()
        interlocks = self.policy.preconditions_for_call(object_node_id, method_node_id)
        if not interlocks:
            return
        refusal = check_preconditions(
            {
                "target": f"{object_node_id}|{method_node_id}",
                "requirements": interlocks,
                "current": await self._current([req["node"] for req in interlocks]),
            }
        )
        if refusal is not None:
            raise ApplicationRefusal(refusal)

    # --- alarm scope ---------------------------------------------------------------

    async def check_alarm_scope(self, condition_id: str) -> None:
        """Refuse an alarm outside the operator policy's alarm scope, before anything is sent.

        ``alarm_sources`` and ``alarm_max_severity`` are what let a deployment give
        an agent the routine alarms and keep the serious ones for a person.
        Checked on the condition itself, read now: the source and severity a list
        once reported are not evidence of what the condition says at the moment
        of acknowledging. A property that could not be read refuses only when the
        policy restricts what it would have said.
        """
        scope = self.policy.alarm_scope()
        if scope is None:
            return
        sources, max_severity = scope
        found = await self.port.condition(condition_id)
        error = None
        kind, source = found["SourceNode"]
        if kind != "value":
            source = None
            if sources is not None:
                error = f"SourceNode: {found['SourceNode'][1]}"
        kind, severity = found["Severity"]
        if kind != "value":
            severity = None
            if max_severity is not None and error is None:
                error = f"Severity: {found['Severity'][1]}"
        refusal = check_alarm_scope(
            {
                "condition_id": condition_id,
                "source": source,
                "severity": severity,
                "error": error,
                "sources": sources,
                "max_severity": max_severity,
            }
        )
        if refusal is not None:
            raise ApplicationRefusal(refusal)


__all__ = ["Model", "ModelPort", "bound_json"]
