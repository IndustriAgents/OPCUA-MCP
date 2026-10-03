"""What a write or a method call to one node may be, decided before anything is sent.

Three pure functions over a node's facts (``node_facts.py``), its engineering
record (``node_metadata.py``) and the request, each driven through both runtimes
by a shared table:

* :func:`plan_write` — ``tests/fixtures/write-plan.json``: send this node's write
  (and as what type), skip it with a per-node status, or refuse the whole batch.
* :func:`plan_call` — ``tests/fixtures/method-plan.json``: whether a method call
  can work at all.
* :func:`write_access` — ``tests/fixtures/write-access.json``: what
  ``read_opcua_nodes`` reports under ``include_write_access``, so an agent can get
  a write right the first time instead of learning the rules from refusals.

Pure, so the tables can pin them exactly; the tool bodies in ``server.py`` do the
reading. A skip and a refusal are different on purpose. A skip is a fact about
*one node* that nobody could write past — a read-only node, a list for a scalar —
and is reported in that node's record while the rest of the batch goes, exactly
as a value that will not convert always was. A refusal is a value outside what
the node allows (a state it does not define, a number outside its own range), and
refuses the whole batch, as every value bound already did.

Every sentence comes from ``contract/tools.json``: ``writeSkips``,
``writeAccessReasons`` and ``errors``.
"""

from __future__ import annotations

from typing import Any

from .contract import CONTRACT
from .errors import message
from .node_facts import CURRENT_WRITE, allows
from .numeric import json_text, numeric_text
from .policy import as_number, format_number

_SKIPS: dict[str, str] = {
    key: value for key, value in CONTRACT["writeSkips"].items() if not key.startswith("$")
}
_REASONS: dict[str, str] = {
    key: value for key, value in CONTRACT["writeAccessReasons"].items() if not key.startswith("$")
}

#: Who set each of the two ranges a node publishes, as ``valueOutOfRange`` says it.
EU_RANGE_SOURCE = "the OPC UA server's own EURange"
INSTRUMENT_RANGE_SOURCE = "the instrument's own InstrumentRange"


def skip_message(key: str, **fields: Any) -> str:
    """One ``writeSkips`` sentence with its placeholders filled in."""
    return _SKIPS[key].format(**fields)


def write_access_reason(key: str, **fields: Any) -> str:
    """One ``writeAccessReasons`` phrase with its placeholders filled in."""
    return _REASONS[key].format(**fields)


def range_refusal(
    node_id: str, number: float, bounds: dict[str, Any], unit: str | None, source: str
) -> str | None:
    """``valueOutOfRange`` for a number outside an inclusive ``{low, high}``, else None."""
    low, high = float(bounds["low"]), float(bounds["high"])
    if low <= number <= high:
        return None
    return message(
        "valueOutOfRange",
        value=format_number(number),
        node_id=node_id,
        low=format_number(low),
        high=format_number(high),
        unit=f" {unit}" if unit else "",
        source=source,
    )


def allowed_states(states: list[dict[str, Any]]) -> str:
    """A node's states as ``valueNotAState`` and ``enumNotAState`` list them."""
    return ", ".join(
        f"{format_number(state['value'])} = {state['label']}"
        if isinstance(state["value"], (int, float)) and not isinstance(state["value"], bool)
        else f"{json_text(state['value'])} = {state['label']}"
        for state in states
    )


def _send(data_type: str | None, array: bool | None, value: Any) -> dict[str, Any]:
    return {"outcome": "send", "data_type": data_type, "array": array, "value": value}


def _skip(status: str, key: str, **fields: Any) -> dict[str, Any]:
    return {"outcome": "skip", "status": status, "error": skip_message(key, **fields)}


def _refuse(error: str) -> dict[str, Any]:
    return {"outcome": "refuse", "error": error}


def _array_for(value_rank: int | None, is_list: bool) -> bool | None:
    """Whether the written value is an array, from the node's ValueRank.

    -1 is a scalar and 0 or more an array; Any (-2) and ScalarOrOneDimension (-3)
    take either, so the value itself says which. Unknown leaves it to wherever
    the type comes from.
    """
    if value_rank is None:
        return None
    if value_rank == -1:
        return False
    if value_rank >= 0:
        return True
    if value_rank in (-2, -3):
        return is_list
    return None


def _state_value(element: Any, states: list[dict[str, Any]]) -> tuple[bool, Any]:
    """One written element mapped onto a node's states: (found, value to write).

    A string that is not a number is a label, matched exactly — case matters,
    because a server's labels are its own spelling and "failed" is not something
    it published. A number, or a numeric string, must be one of the state values.
    Anything else names no state.
    """
    if isinstance(element, str) and numeric_text(element) is None:
        for state in states:
            if state["label"] == element:
                return True, state["value"]
        return False, None
    if isinstance(element, bool) or not isinstance(element, (int, float, str)):
        return False, None
    number = as_number(element)
    if number is None or not number.is_integer():
        return False, None
    for state in states:
        value = state["value"]
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value == number:
            return True, value
    return False, None


def plan_write(
    request: dict[str, Any],
    facts: dict[str, Any] | None,
    engineering: dict[str, Any] | None,
    options: dict[str, Any],
) -> dict[str, Any]:
    """The plan for one node's write. See the module docstring and write-plan.json.

    ``facts`` is the node's facts record or None; ``engineering`` its AnalogInfo
    JSON or None; ``options`` carries ``allow_out_of_range``. Each step is a fact
    the node published, and a missing fact skips its step: the server enforces
    its own rules and answers for itself.
    """
    node_id = str(request.get("node_id", ""))
    value = request.get("value")
    requested = request.get("data_type") or None

    # An unknown node is the server's to answer, exactly as before.
    if facts is None or facts.get("status") != "Good":
        return _send(requested, None, value)

    node_class = facts.get("node_class")
    if node_class != "Variable":
        return _skip("BadNodeClassInvalid", "notVariable", node_id=node_id, node_class=node_class)
    if allows(facts.get("access_level"), CURRENT_WRITE) is False:
        return _skip("BadNotWritable", "notWritable", node_id=node_id)
    if allows(facts.get("user_access_level"), CURRENT_WRITE) is False:
        return _skip("BadUserAccessDenied", "notWritableForUser", node_id=node_id)

    declared = facts.get("data_type")
    if requested and declared and requested != declared:
        return _skip(
            "BadTypeMismatch",
            "typeMismatch",
            node_id=node_id,
            node_type=declared,
            data_type=requested,
        )
    data_type = requested or declared

    is_list = isinstance(value, list)
    value_rank = facts.get("value_rank")
    if value_rank == -1 and is_list:
        return _skip("BadTypeMismatch", "needsScalar", node_id=node_id)
    if value_rank is not None and value_rank >= 0 and not is_list:
        return _skip("BadTypeMismatch", "needsArray", node_id=node_id, value_rank=value_rank)
    dimensions = facts.get("array_dimensions")
    if value_rank == 1 and dimensions and dimensions[0] > 0 and len(value) > dimensions[0]:
        return _skip(
            "BadOutOfRange", "arrayTooLong", node_id=node_id, limit=dimensions[0], count=len(value)
        )
    array = _array_for(value_rank, is_list)
    if value_rank is not None and value_rank >= 2 and not requested:
        # Multi-dimensional: the dimensions come from the current value, so the
        # type does too, as it always has.
        data_type, array = None, None

    elements = list(value) if is_list else [value]
    states = facts.get("states")
    two_state = facts.get("two_state")
    if states:
        resolved_elements = []
        for element in elements:
            found, mapped = _state_value(element, states)
            if not found:
                return _refuse(
                    message(
                        "valueNotAState",
                        value=json_text(element),
                        node_id=node_id,
                        allowed=allowed_states(states),
                    )
                )
            resolved_elements.append(mapped)
    elif two_state:
        resolved_elements = [
            True
            if element == two_state["true"]
            else False
            if element == two_state["false"]
            else element
            for element in elements
        ]
    else:
        resolved_elements = elements

    if engineering:
        unit = engineering.get("unit")
        eu_range = engineering.get("eu_range")
        instrument_range = engineering.get("instrument_range")
        for element in resolved_elements:
            number = as_number(element)
            if number is None:
                continue
            # The EURange is what the plant expects in normal operation, and the
            # override exists for writing outside it on purpose. The
            # InstrumentRange is what the device can represent at all, and no
            # deployment writes past that on purpose — so it holds regardless.
            refusal = None
            if eu_range and not options.get("allow_out_of_range"):
                refusal = range_refusal(node_id, number, eu_range, unit, EU_RANGE_SOURCE)
            if refusal is None and instrument_range:
                refusal = range_refusal(
                    node_id, number, instrument_range, unit, INSTRUMENT_RANGE_SOURCE
                )
            if refusal is not None:
                return _refuse(refusal)

    return _send(data_type, array, resolved_elements if is_list else resolved_elements[0])


def plan_call(call: dict[str, Any]) -> str | None:
    """Why a method call cannot work, or None to send it. See method-plan.json.

    Each check reads a fact the two nodes published and is skipped when it could
    not be read. The argument count is checked even when the method node itself
    could not be inspected: its InputArguments were read separately, and a count
    that disagrees with them is a call the server would turn down either way.
    """
    method_facts = call.get("method_facts")
    object_facts = call.get("object_facts")
    method_node_id = call["method_node_id"]
    object_node_id = call["object_node_id"]

    if (
        method_facts
        and method_facts.get("status") == "Good"
        and method_facts.get("node_class") != "Method"
    ):
        return message(
            "methodNotAMethod",
            method_node_id=method_node_id,
            node_class=method_facts.get("node_class"),
        )
    if (
        object_facts
        and object_facts.get("status") == "Good"
        and object_facts.get("node_class") not in {"Object", "ObjectType"}
    ):
        return message(
            "methodObjectNotAnObject",
            object_node_id=object_node_id,
            node_class=object_facts.get("node_class"),
        )
    if method_facts and method_facts.get("executable") is False:
        return message("methodNotExecutable", method_node_id=method_node_id)
    if method_facts and method_facts.get("user_executable") is False:
        return message("methodNotExecutableForUser", method_node_id=method_node_id)
    if call.get("on_object") is False:
        return message(
            "methodNotOnObject", method_node_id=method_node_id, object_node_id=object_node_id
        )
    declared = call.get("declared_arguments")
    given = call.get("given_arguments", 0)
    if declared is not None and declared != given:
        return message(
            "methodArgumentCount", method_node_id=method_node_id, expected=declared, count=given
        )
    return None


def write_access(entry: dict[str, Any]) -> dict[str, Any]:
    """What ``read_opcua_nodes`` reports as one node's ``write_access``. See write-access.json.

    ``entry`` carries the node's facts and engineering record, and what the policy
    says about writing it (``policy``: ``tool_refusal``, ``operator``,
    ``allowlisted``, ``bound``, ``allow_out_of_range``). Every key is always
    present, so a client can read the shape without guarding each field.
    """
    node_id = entry["node_id"]
    facts = entry.get("facts")
    engineering = entry.get("engineering") or {}
    policy = entry["policy"]
    operator = bool(policy.get("operator"))
    bound = (policy.get("bound") or None) if operator else None

    reason = None
    if facts is None:
        reason = write_access_reason("unknown")
    elif facts.get("status") != "Good":
        reason = write_access_reason("unreadable", status=facts.get("status"))
    elif facts.get("node_class") != "Variable":
        reason = write_access_reason("notVariable", node_class=facts.get("node_class"))
    elif policy.get("tool_refusal"):
        reason = policy["tool_refusal"]
    elif operator and not policy.get("allowlisted"):
        reason = message("nodeNotWritable", node_id=node_id)
    elif allows(facts.get("access_level"), CURRENT_WRITE) is False:
        reason = write_access_reason("readOnly")
    elif allows(facts.get("user_access_level"), CURRENT_WRITE) is False:
        reason = write_access_reason("readOnlyForUser")

    known = facts is not None and facts.get("status") == "Good"
    data_type = facts.get("data_type") if known else None
    value_rank = facts.get("value_rank") if known else None
    # Unlike a write's plan there is no value to settle Any or ScalarOrOneDimension,
    # so those are null: the node takes either.
    array = None
    if value_rank == -1:
        array = False
    elif value_rank is not None and value_rank >= 0:
        array = True
    states = None
    if known:
        states = facts.get("states")
        two_state = facts.get("two_state")
        if not states and two_state:
            states = [
                {"value": True, "label": two_state["true"]},
                {"value": False, "label": two_state["false"]},
            ]
        states = states or None

    # The tightest of everything that bounds a value: the plant's EURange (unless
    # the override lifts it), the instrument's range (which nothing lifts), and
    # the operator's own min and max.
    ranges = []
    if engineering.get("eu_range") and not policy.get("allow_out_of_range"):
        ranges.append((engineering["eu_range"]["low"], engineering["eu_range"]["high"]))
    if engineering.get("instrument_range"):
        ranges.append(
            (engineering["instrument_range"]["low"], engineering["instrument_range"]["high"])
        )
    lows = [low for low, _ in ranges]
    highs = [high for _, high in ranges]
    if bound:
        if bound.get("min") is not None:
            lows.append(bound["min"])
        if bound.get("max") is not None:
            highs.append(bound["max"])

    return {
        "allowed": reason is None,
        "reason": reason,
        "data_type": data_type,
        "array": array,
        "states": states,
        "min": max(lows) if lows else None,
        "max": min(highs) if highs else None,
        "allowed_values": list(bound["enum"]) if bound and bound.get("enum") else None,
        "max_change": bound.get("max_change") if bound else None,
    }


__all__ = [
    "EU_RANGE_SOURCE",
    "INSTRUMENT_RANGE_SOURCE",
    "allowed_states",
    "plan_call",
    "plan_write",
    "range_refusal",
    "skip_message",
    "write_access",
    "write_access_reason",
]
