"""Check a call's arguments against the contract's own input schema.

``validation.ts`` is the Node half and must accept and reject exactly the same
calls, because the schema being checked is the same document.

This is deliberately *not* a JSON Schema implementation. The contract uses a
handful of keywords and a validator that covers only those is one that can be
read in full and mirrored in another language without a dependency on either
side. A keyword appearing in the contract that is not handled here would be
silently ignored, so :data:`SUPPORTED_KEYWORDS` is asserted against the contract
by ``tests/unit/test_contract.py``.

Three of them were added later, and each closes a hole the six original ones left
(issue #114):

``additionalProperties``
    No input schema forbade extras, so ``{"node_clas": "Variable"}`` was accepted
    and browsed with the default class. A misspelled *optional* argument changed
    behaviour instead of producing a refusal the model could correct from — and
    models misspell optional arguments. The refusal names the tool's real
    arguments, because "no such argument" without the list is a riddle.

``enum``
    ``node_class`` and ``data_type`` are fixed sets, and both used to fail
    downstream and unhelpfully: an unknown node class silently matched nothing
    and returned an empty list, which is indistinguishable from a subtree that
    really is empty.

``minimum``
    Every numeric argument is floored at zero. A negative is never meaningful for
    any of them and was silently clamped, which is how a caller computing an
    offset wrongly gets a plausible answer and never finds out. The *ceilings*
    stay clamps rather than refusals: they are documented caps on how much work
    one call may ask for, and the result says when one was hit.

Why check at all, when each server already has a schema-shaped thing of its own:
the Node runtime had none (the low-level MCP ``Server`` does not validate against
the advertised ``inputSchema``), so a malformed call reached ``node-opcua`` as
whatever the client sent. The Python runtime validated against a *different*
schema — the one ``MCPServer`` derives from the function signature — which knows
that ``nodes`` is a list of objects and nothing about what belongs in one. Both
now answer to the contract, and word the refusal identically.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import message

#: Every JSON Schema keyword this validator understands. The contract may not use
#: one that is missing here; a unit test enforces that.
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "items",
        "properties",
        "required",
        "minItems",
        "description",
        "additionalProperties",
        "enum",
        "minimum",
    }
)

#: How each declared type is named in a refusal, and what counts as one.
_TYPE_NAMES = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
}


def _matches(value: Any, declared: str) -> bool:
    """Whether ``value`` is of the declared JSON type.

    ``bool`` is excluded from the numeric types explicitly: it is an ``int`` in
    Python and is not in JSON, and letting ``true`` through as an integer is
    exactly the coercion the typed-argument work (#10) removed from method calls.
    An ``integer`` accepts a whole-valued float because JSON has one number type
    and ``5.0`` off the wire is the integer 5 — which is what JavaScript sends
    and so what the Node half must accept too.
    """
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, str)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    # A type the contract declares and this validator does not know. Accepting is
    # the safe direction: the alternative is refusing a call the contract allows.
    return True


def _expected(schema: dict) -> str:
    """How a declared type is worded in a refusal: 'an array of strings'."""
    declared = schema.get("type")
    if isinstance(declared, list):
        # A union ('string' or 'null') is only used in result shapes today, but
        # wording it is cheaper than being surprised by it.
        return " or ".join(_TYPE_NAMES.get(entry, entry) for entry in declared)
    if declared == "array":
        item_type = (schema.get("items") or {}).get("type")
        if item_type == "string":
            return "an array of strings"
        if item_type == "object":
            return "an array of objects"
        return "an array"
    return _TYPE_NAMES.get(declared, str(declared))


def validate_arguments(tool: str, schema: dict, arguments: Any) -> None:
    """Raise ``ValueError`` if ``arguments`` do not satisfy the contract schema.

    The message is a contract one, so the Node runtime refuses the same call with
    the same sentence.
    """
    if not isinstance(arguments, dict):
        raise ValueError(
            message("wrongType", tool=tool, argument="arguments", expected="an object")
        )
    _check_object(tool, schema, arguments, prefix="")


def _check_object(tool: str, schema: dict, value: dict, prefix: str) -> None:
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        # Before the per-property checks, so a typo is reported as the typo it is
        # rather than as whatever the misspelled name happens to resemble.
        for name in value:
            if name not in properties:
                raise ValueError(
                    message(
                        "unknownArgument",
                        tool=tool,
                        argument=f"{prefix}{name}",
                        allowed=", ".join(properties) or "no arguments",
                    )
                )

    for name in schema.get("required", []):
        if value.get(name) is None:
            raise ValueError(message("missingArgument", tool=tool, argument=f"{prefix}{name}"))

    for name, subschema in properties.items():
        if name not in value or value[name] is None:
            # Absent is not wrong: `required` above has already refused the ones
            # that had to be there, and everything else carries a default.
            continue
        _check_value(tool, subschema, value[name], f"{prefix}{name}")


def _check_value(tool: str, schema: dict, value: Any, path: str) -> None:
    declared = schema.get("type")
    if declared is None:
        # `value` in a write entry deliberately declares no type: what may be
        # written is decided by the target node, not by this schema.
        return
    if isinstance(declared, str) and not _matches(value, declared):
        raise ValueError(message("wrongType", tool=tool, argument=path, expected=_expected(schema)))

    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        raise ValueError(
            message(
                "notAllowedValue",
                tool=tool,
                argument=path,
                allowed=", ".join(json.dumps(item) for item in allowed),
                value=json.dumps(value),
            )
        )

    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        raise ValueError(
            message(
                "belowMinimum",
                tool=tool,
                argument=path,
                minimum=minimum,
                value=json.dumps(value),
            )
        )

    if declared == "array":
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            # The one rule with its own sentence, because it is the one a model
            # trips over: an empty list is a well-formed array and a meaningless
            # request, and "must be a non-empty array" says what to do about it.
            raise ValueError(message("emptyArray", tool=tool, argument=path))
        items = schema.get("items")
        if items is not None:
            for index, element in enumerate(value):
                _check_value(tool, items, element, f"{path}[{index}]")

    elif declared == "object":
        _check_object(tool, schema, value, prefix=f"{path}.")


__all__ = ["SUPPORTED_KEYWORDS", "validate_arguments"]
