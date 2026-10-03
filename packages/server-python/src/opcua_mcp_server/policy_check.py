"""The deployment's policy measured against the connected server, and its interlocks.

Three pure functions, each driven through both runtimes by a shared table:

* :func:`policy_findings` — ``tests/fixtures/policy-check.json``: what the check
  run on every session says about the policy, given what each entry resolved to
  and what the server says about the nodes it names. A policy file is written
  once and the plant changes under it; an allowlisted node that turned read-only,
  a method that was switched off, a browse path that now matches two nodes —
  each is an entry that allows nothing the server will accept, and nobody finds
  out until the one write that mattered fails. A finding never widens or disables
  anything: saying so is the whole point.
* :func:`check_preconditions` — ``tests/fixtures/preconditions.json``: whether
  the interlocks the operator policy ties to a target hold right now.
* :func:`check_alarm_scope` — ``tests/fixtures/alarm-scope.json``: whether an
  alarm is one the operator policy lets an agent acknowledge or change.

The reading that feeds them lives in ``policy_resolution.py`` and ``server.py``.
Every sentence is ``contract/tools.json`` -> ``policyCheck.messages`` or
``errors``.
"""

from __future__ import annotations

from typing import Any

from .contract import CONTRACT
from .errors import message
from .node_facts import CURRENT_WRITE, allows
from .numeric import json_text
from .policy import _same_json_value, as_number, format_number
from .write_plan import allowed_states

POLICY_CHECK = CONTRACT["policyCheck"]
_MESSAGES: dict[str, str] = POLICY_CHECK["messages"]


def finding_message(name: str, **fields: Any) -> str:
    """One ``policyCheck.messages`` sentence with its placeholders filled in."""
    return _MESSAGES[name].format(**fields)


def _finding(entry: str, name: str, **fields: Any) -> dict[str, str]:
    # `name`, not `key`: boundOutsideRange has a {key} placeholder of its own.
    return {"entry": entry, "problem": finding_message(name, entry=entry, **fields)}


def _good(facts: dict[str, Any] | None) -> bool:
    return facts is not None and facts.get("status") == "Good"


def _unresolved(item: dict[str, Any]) -> dict[str, str]:
    if item.get("unresolved_reason") == "pathAmbiguous":
        return _finding(
            item["entry"], "pathAmbiguous", segment=item.get("segment"), parent=item.get("parent")
        )
    return _finding(item["entry"], "unresolved")


def json_or_number(value: Any) -> str:
    """A value as a message names it: a number as format_number writes it, else JSON."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(float(value))
    return json_text(value)


def _writable_findings(item: dict[str, Any]) -> list[dict[str, str]]:
    entry = item["entry"]
    node_id = item.get("node_id")
    if node_id is None:
        return [_unresolved(item)]
    facts = item.get("facts")
    if facts is None:
        return []
    if not _good(facts):
        return [_finding(entry, "unknownNode", node_id=node_id, status=facts.get("status"))]
    if facts.get("node_class") != "Variable":
        return [_finding(entry, "notVariable", node_id=node_id, node_class=facts.get("node_class"))]

    found = []
    # One or the other: a node nobody can write says nothing more by also being
    # one this user cannot.
    if allows(facts.get("access_level"), CURRENT_WRITE) is False:
        found.append(_finding(entry, "readOnly", node_id=node_id))
    elif allows(facts.get("user_access_level"), CURRENT_WRITE) is False:
        found.append(_finding(entry, "readOnlyForUser", node_id=node_id))

    bound = item.get("bound") or {}
    eu_range = (item.get("engineering") or {}).get("eu_range")
    if eu_range:
        low, high = float(eu_range["low"]), float(eu_range["high"])
        for key, outside in (
            ("min", lambda value: value < low),
            ("max", lambda value: value > high),
        ):
            value = bound.get(key)
            if value is not None and outside(float(value)):
                found.append(
                    _finding(
                        entry,
                        "boundOutsideRange",
                        key=key,
                        value=format_number(float(value)),
                        low=format_number(low),
                        high=format_number(high),
                    )
                )

    states = facts.get("states")
    if bound.get("enum") and states:
        for candidate in bound["enum"]:
            if not any(_names_state(candidate, state) for state in states):
                found.append(
                    _finding(
                        entry,
                        "enumNotAState",
                        value=json_or_number(candidate),
                        allowed=allowed_states(states),
                    )
                )
    return found


def _names_state(candidate: Any, state: dict[str, Any]) -> bool:
    """Whether an enum entry names a state: a number by its value, a string by its label."""
    if isinstance(candidate, str):
        return candidate == state["label"]
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        return False
    value = state["value"]
    return not isinstance(value, bool) and value == candidate


def _method_finding(item: dict[str, Any]) -> dict[str, str] | None:
    """The first thing wrong with one callable_methods entry, or None."""
    entry = item["entry"]
    object_node_id = item.get("object_node_id")
    method_node_id = item.get("method_node_id")
    if object_node_id is None or method_node_id is None:
        return _unresolved(item)
    object_facts = item.get("object_facts")
    method_facts = item.get("method_facts")
    if object_facts is not None and not _good(object_facts):
        return _finding(
            entry, "unknownNode", node_id=object_node_id, status=object_facts.get("status")
        )
    if method_facts is not None and not _good(method_facts):
        return _finding(
            entry, "unknownNode", node_id=method_node_id, status=method_facts.get("status")
        )
    if _good(method_facts) and method_facts.get("node_class") != "Method":
        return _finding(
            entry,
            "methodNotAMethod",
            method_node_id=method_node_id,
            node_class=method_facts.get("node_class"),
        )
    if _good(object_facts) and object_facts.get("node_class") not in {"Object", "ObjectType"}:
        return _finding(
            entry,
            "methodObjectNotAnObject",
            object_node_id=object_node_id,
            node_class=object_facts.get("node_class"),
        )
    if method_facts and method_facts.get("executable") is False:
        return _finding(entry, "methodNotExecutable")
    if method_facts and method_facts.get("user_executable") is False:
        return _finding(entry, "methodNotExecutableForUser")
    if item.get("on_object") is False:
        return _finding(
            entry,
            "methodNotOnObject",
            method_node_id=method_node_id,
            object_node_id=object_node_id,
        )
    return None


def policy_findings(check: dict[str, Any]) -> list[dict[str, str]]:
    """Every problem the policy check finds, in policy order. See policy-check.json.

    Sections in the order the policy file lists them — writable, methods,
    subtrees, deny, alarm sources, preconditions — and within each, in the order
    written, so the report reads alongside the file. A writable entry stops at
    the first finding that makes the rest meaningless (it does not resolve, the
    server does not have it, it is not a Variable) and otherwise reports
    everything that applies; a method entry reports its first problem.
    """
    found: list[dict[str, str]] = []
    for item in check.get("writable") or []:
        found.extend(_writable_findings(item))
    for item in check.get("methods") or []:
        finding = _method_finding(item)
        if finding is not None:
            found.append(finding)
    for item in check.get("subtrees") or []:
        outcome = item.get("outcome")
        if outcome == "empty":
            found.append(_finding(item["entry"], "subtreeEmpty"))
        elif outcome == "tooLarge":
            found.append(_finding(item["entry"], "subtreeTooLarge", limit=item.get("limit")))
        elif outcome == "unresolved":
            found.append(_finding(item["entry"], "unresolved"))
    for item in check.get("deny") or []:
        outcome = item.get("outcome")
        if outcome == "unresolved":
            found.append(_finding(item["entry"], "unresolved"))
        elif outcome == "tooLarge":
            found.append(_finding(item["entry"], "denyTooLarge", limit=item.get("limit")))
        elif outcome == "failed":
            found.append(_finding(item["entry"], "denyFailed", reason=item.get("reason")))
    for section in ("alarm_sources", "preconditions"):
        for item in check.get(section) or []:
            if item.get("node_id") is None:
                found.append(_finding(item["entry"], "unresolved"))
    return found


# --- preconditions ----------------------------------------------------------------


def requirement_text(requirement: dict[str, Any]) -> str:
    """One requirement as ``preconditionNotMet`` states it: ``<node> equals true``."""
    node = requirement["node"]
    if requirement.get("equals") is not None:
        return f"{node} equals {json_or_number(requirement['equals'])}"
    if requirement.get("in") is not None:
        return f"{node} is one of " + ", ".join(json_or_number(item) for item in requirement["in"])
    low, high = requirement.get("min"), requirement.get("max")
    if low is not None and high is not None:
        return f"{node} is between {format_number(float(low))} and {format_number(float(high))}"
    if low is not None:
        return f"{node} is at least {format_number(float(low))}"
    return f"{node} is at most {format_number(float(high))}"


def _holds(requirement: dict[str, Any], current: dict[str, Any] | None) -> bool:
    if current is None or current.get("status") != "Good":
        return False
    value = current.get("value")
    if requirement.get("equals") is not None:
        return _same_json_value(value, requirement["equals"])
    if requirement.get("in") is not None:
        return any(_same_json_value(value, candidate) for candidate in requirement["in"])
    number = as_number(value)
    if number is None:
        return False
    low, high = requirement.get("min"), requirement.get("max")
    return (low is None or number >= float(low)) and (high is None or number <= float(high))


def _current_text(current: dict[str, Any] | None) -> str:
    if current is None:
        return "unresolvable"
    if current.get("status") != "Good":
        return f"unreadable ({current.get('status')})"
    return json_or_number(current.get("value"))


def check_preconditions(check: dict[str, Any]) -> str | None:
    """Why a guarded write or call may not go now, or None. See preconditions.json.

    Every requirement must hold, and the first that does not is the one named. A
    requirement holds only on a Good reading: an interlock whose state cannot be
    read is not an interlock that is satisfied, and a node that did not resolve
    at all (``None``) is the same — a precondition is a policy decision, so it
    fails closed.
    """
    current = check.get("current") or {}
    for requirement in check.get("requirements") or []:
        reading = current.get(requirement["node"])
        if _holds(requirement, reading):
            continue
        return message(
            "preconditionNotMet",
            target=check["target"],
            requirement=requirement_text(requirement),
            node_id=requirement["node"],
            current=_current_text(reading),
        )
    return None


# --- alarm scope ------------------------------------------------------------------


def check_alarm_scope(check: dict[str, Any]) -> str | None:
    """Why an alarm may not be acknowledged or changed under the policy, or None.

    See alarm-scope.json. An alarm whose source or severity could not be read
    when the policy restricts it is refused, not waved through: an agent is not
    to acknowledge on the policy's behalf an alarm the policy cannot be checked
    against.
    """
    condition_id = check["condition_id"]
    if check.get("error") is not None:
        return message("alarmScopeUnreadable", condition_id=condition_id, reason=check["error"])
    sources = check.get("sources")
    source = check.get("source")
    if sources is not None and (source is None or source not in sources):
        return message(
            "alarmSourceNotAllowed",
            condition_id=condition_id,
            source_node_id=source if source is not None else "an unknown source",
        )
    limit = check.get("max_severity")
    severity = check.get("severity")
    if limit is not None and (severity is None or severity > limit):
        return message(
            "alarmTooSevere",
            condition_id=condition_id,
            severity=severity if severity is not None else "unknown",
            limit=limit,
        )
    return None


__all__ = [
    "POLICY_CHECK",
    "check_alarm_scope",
    "check_preconditions",
    "finding_message",
    "json_or_number",
    "policy_findings",
    "requirement_text",
]
