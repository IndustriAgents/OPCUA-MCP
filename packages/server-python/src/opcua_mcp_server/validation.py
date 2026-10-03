"""Draft 2020-12 validation with stable, contract-owned refusals.

The schema library decides validity. This module only orders and translates its
errors, keeping library prose out of the MCP boundary. Optional nullable fields
in the advertised contract preserve the existing default-on-null behavior.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, validators

from .errors import message
from .generated_contract import SCHEMA_KEYWORDS
from .numeric import json_text

SUPPORTED_KEYWORDS = frozenset(SCHEMA_KEYWORDS)
_TYPE_NAMES = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
    "null": "null",
}


def _number(_checker, value):
    return not isinstance(value, bool) and (
        isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    )


Validator = validators.extend(
    Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine("number", _number)
)


@lru_cache(maxsize=256)
def _validator(schema_json: str):
    return Validator(json.loads(schema_json))


def _expected(schema):
    declared = schema.get("type")
    if isinstance(declared, list):
        # Nullable optional inputs use null as "apply default". Preserve their
        # existing correction message for a non-null value of the wrong type.
        non_null = [t for t in declared if t != "null"]
        if len(non_null) == 1:
            return _expected({**schema, "type": non_null[0]})
        return " or ".join(_TYPE_NAMES.get(t, t) for t in non_null)
    if declared == "array":
        item_type = schema.get("items", {}).get("type")
        if item_type == "string":
            return "an array of strings"
        if item_type == "object":
            return "an array of objects"
    return _TYPE_NAMES.get(declared, str(declared))


def _at(schema, instance, path):
    order = []
    parent = schema
    for item in path:
        parent = schema
        if isinstance(item, int):
            order.extend((6, item))
            schema = schema.get("items", {})
        else:
            keys = list(schema.get("properties", {}))
            order.extend((6, keys.index(item) if item in keys else len(keys)))
            schema = schema.get("properties", {}).get(item, {})
        instance = instance[item]
    return schema, parent, instance, order


def _path(path):
    return "".join(
        f"[{p}]" if isinstance(p, int) else ("." if i else "") + p for i, p in enumerate(path)
    )


class ValidationRefusal(ValueError):
    """A schema violation with a stable project code and argument path."""

    def __init__(self, code, argument, text):
        super().__init__(text)
        self.code, self.argument = code, argument


def validate_arguments(tool: str, schema: dict, arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise ValidationRefusal(
            "wrongType",
            "arguments",
            message("wrongType", tool=tool, argument="arguments", expected="an object"),
        )
    failures = []
    for error in _validator(json.dumps(schema)).iter_errors(arguments):
        path = list(error.absolute_path)
        leaf, parent, value, order = _at(schema, arguments, path)
        keyword = error.validator
        argument = _path(path)
        params = {"tool": tool, "argument": argument}
        code = "schemaConstraint"
        rank = 3
        if keyword == "type":
            rank, code = 0, "wrongType"
            if value is None and path and path[-1] in parent.get("required", []):
                code, rank = "missingArgument", 5
                order = order[:-2]
            else:
                params["expected"] = _expected(leaf)
        elif keyword == "required":
            rank, code = 5, "missingArgument"
            missing = next(k for k in leaf["required"] if k not in value)
            params["argument"] = argument + ("." if argument else "") + missing
        elif keyword == "additionalProperties":
            rank, code = 4, "unknownArgument"
            props = leaf.get("properties", {})
            unknown = next(k for k in value if k not in props)
            params.update(
                argument=argument + ("." if argument else "") + unknown,
                allowed=", ".join(props) or "no arguments",
            )
        elif keyword == "enum":
            rank, code = 1, "notAllowedValue"
            params.update(
                allowed=", ".join(json_text(v) for v in leaf["enum"] if v is not None),
                value=json_text(value),
            )
        elif keyword == "minimum":
            rank, code = 2, "belowMinimum"
            params.update(minimum=json_text(leaf["minimum"]), value=json_text(value))
        elif keyword == "minItems" and leaf["minItems"] == 1:
            code = "emptyArray"
        elif keyword == "maxItems":
            code = "tooManyItems"
            params.update(limit=leaf["maxItems"], count=len(value))
        if keyword == "uniqueItems":
            # Ajv may omit uniqueness errors when an element has a wrong type.
            # Prefer element corrections on both engines in that case.
            rank = 7
        if code == "schemaConstraint":
            params["constraint"] = keyword
            if not argument:
                params["argument"] = "arguments"
        if code == "missingArgument":
            required_schema = parent if keyword == "type" else leaf
            required_name = path[-1] if keyword == "type" else missing
            order.extend((rank, required_schema["required"].index(required_name)))
        else:
            order.append(rank)
        failures.append((tuple(order), code, params))
    if failures:
        _, code, params = min(failures, key=lambda item: item[0])
        raise ValidationRefusal(code, params["argument"], message(code, **params))


__all__ = ["SUPPORTED_KEYWORDS", "ValidationRefusal", "validate_arguments"]
