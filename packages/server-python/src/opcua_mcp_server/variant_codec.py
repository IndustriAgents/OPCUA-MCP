"""Strict MCP JSON to OPC UA Variant conversion for writes.

What a JSON value becomes on the wire is decided here, and it has to be decided
the same way by ``variant-codec.ts``: a value one runtime writes and the other
refuses — or, worse, writes differently — is a different plant depending on which
package was installed (#157). ``tests/fixtures/write-coercion.json`` is the one
table both are held to, messages included.

The rules, where each runtime's native conversion used to decide:

* A numeric string follows JSON's number grammar (``numeric.py``); no ``"0x10"``,
  ``"1_000"``, ``"inf"`` or ``""``.
* A boolean is not a number and a number is not a string: ``true`` is refused for
  a Double and ``42`` for a String, rather than each runtime spelling it its own
  way (``"true"``/``"True"``, ``"1"``/``"1.0"``).
* A scalar node takes a scalar. ``[5]`` is not 5.
* An integer given as a JSON number must be one JSON carries exactly
  (±2**53-1); a larger one has already been rounded by a JavaScript parser, so it
  is refused and must be sent as a decimal string.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from opcua import ua

from .datetimes import parse_iso_datetime
from .errors import message
from .limits import MAX_BYTE_STRING_BYTES, LimitExceeded
from .numeric import MAX_SAFE_INTEGER, exact_integer, js_number, json_text, numeric_text

_INTEGER_RANGES = {
    ua.VariantType.SByte: (-(2**7), 2**7 - 1),
    ua.VariantType.Byte: (0, 2**8 - 1),
    ua.VariantType.Int16: (-(2**15), 2**15 - 1),
    ua.VariantType.UInt16: (0, 2**16 - 1),
    ua.VariantType.Int32: (-(2**31), 2**31 - 1),
    ua.VariantType.UInt32: (0, 2**32 - 1),
    ua.VariantType.Int64: (-(2**63), 2**63 - 1),
    ua.VariantType.UInt64: (0, 2**64 - 1),
}

_WIDE_INTEGERS = {ua.VariantType.Int64, ua.VariantType.UInt64}

#: The largest finite IEEE-754 single. Past it a Float is infinity on the wire —
#: node-opcua writes that, python-opcua raises — so it is refused before either.
_FLOAT_MAX = 3.4028234663852886e38

_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_JSON_WHITESPACE = " \t\n\r"


def _is_number(raw: Any) -> bool:
    return isinstance(raw, (int, float)) and not isinstance(raw, bool)


def _is_finite(raw: int | float) -> bool:
    """Whether a JSON number is one a double can hold.

    An integer too large for a double is Infinity to a JavaScript parser, so it
    is treated as the Node runtime sees it.
    """
    try:
        return math.isfinite(float(raw))
    except OverflowError:
        return False


def _cannot(raw: Any, variant_type: ua.VariantType) -> ValueError:
    return ValueError(f"Cannot convert {json_text(raw)} to {variant_type.name}")


def _out_of_range(text: str, variant_type: ua.VariantType) -> ValueError:
    return ValueError(f"{text} is outside the {variant_type.name} range")


def _integer(raw: Any, variant_type: ua.VariantType) -> int:
    minimum, maximum = _INTEGER_RANGES[variant_type]
    if _is_number(raw):
        if not _is_finite(raw):
            raise ValueError(f"The number is outside the {variant_type.name} range")
        if isinstance(raw, float) and not raw.is_integer():
            raise _cannot(raw, variant_type)
        value = int(raw)
        if abs(value) > MAX_SAFE_INTEGER and variant_type in _WIDE_INTEGERS:
            raise ValueError(
                f"A JSON number beyond ±{MAX_SAFE_INTEGER} has lost precision before it "
                f"arrives; send {variant_type.name} values this large as decimal strings"
            )
        if not minimum <= value <= maximum:
            raise _out_of_range(json_text(raw), variant_type)
        return value

    text = numeric_text(raw) if isinstance(raw, str) else None
    value = exact_integer(text) if text is not None else None
    if value is None:
        raise _cannot(raw, variant_type)
    if value == "too large" or not minimum <= value <= maximum:
        raise _out_of_range(text, variant_type)
    return value


def _boolean(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if _is_number(raw) and raw in (0, 1):
        return raw == 1
    if isinstance(raw, str):
        normalized = raw.strip(_JSON_WHITESPACE).lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise _cannot(raw, ua.VariantType.Boolean)


def _floating(raw: Any, variant_type: ua.VariantType) -> float:
    if _is_number(raw):
        if not _is_finite(raw):
            raise ValueError(f"The number is outside the {variant_type.name} range")
        value = float(raw)
    else:
        text = numeric_text(raw) if isinstance(raw, str) else None
        if text is None:
            raise _cannot(raw, variant_type)
        value = float(text)
        if not math.isfinite(value):
            raise _out_of_range(text, variant_type)
    if variant_type == ua.VariantType.Float and abs(value) > _FLOAT_MAX:
        raise _out_of_range(js_number(value), variant_type)
    return value


def _string_only(raw: Any, variant_type: ua.VariantType) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{variant_type.name} values must be a JSON string")
    return raw


def _scalar(raw: Any, variant_type: ua.VariantType) -> Any:
    if raw is None:
        raise ValueError(f"Cannot write null as {variant_type.name}")
    if variant_type in _INTEGER_RANGES:
        return _integer(raw, variant_type)
    if variant_type == ua.VariantType.Boolean:
        return _boolean(raw)
    if variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
        return _floating(raw, variant_type)
    if variant_type == ua.VariantType.String:
        return _string_only(raw, variant_type)
    if variant_type == ua.VariantType.DateTime:
        if isinstance(raw, datetime):
            return raw
        if not isinstance(raw, str):
            raise ValueError(
                "DateTime values must be an ISO 8601 string, e.g. 2026-04-23T17:40:00Z"
            )
        return parse_iso_datetime(raw)
    if variant_type == ua.VariantType.Guid:
        if not isinstance(raw, str) or not _GUID.fullmatch(raw):
            raise ValueError(f"{json_text(raw)} is not a Guid")
        return UUID(raw)
    if variant_type == ua.VariantType.ByteString:
        if isinstance(raw, bytes):
            decoded = raw
        elif not isinstance(raw, str) or not _BASE64.fullmatch(raw):
            raise ValueError("ByteString values must be standard base64")
        else:
            try:
                decoded = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("ByteString values must be standard base64") from exc
        # A refusal of the request, not a conversion failure of one value: a
        # write reports a conversion failure as that node's status and sends the
        # rest, while this has to stop the batch before anything is sent (#139).
        if len(decoded) > MAX_BYTE_STRING_BYTES:
            raise LimitExceeded(
                message("byteStringTooLong", size=len(decoded), limit=MAX_BYTE_STRING_BYTES)
            )
        return decoded
    if variant_type == ua.VariantType.NodeId:
        return ua.NodeId.from_string(_string_only(raw, variant_type))
    if variant_type == ua.VariantType.LocalizedText:
        if isinstance(raw, ua.LocalizedText):
            return raw
        return ua.LocalizedText(_string_only(raw, variant_type))
    if variant_type == ua.VariantType.QualifiedName:
        if isinstance(raw, ua.QualifiedName):
            return raw
        return ua.QualifiedName(_string_only(raw, variant_type))
    raise ValueError(f"Writes to OPC UA {variant_type.name} values are not supported safely")


def _no_constants(name: str) -> Any:
    """``json.loads`` accepts ``NaN`` and ``Infinity``; ``JSON.parse`` does not."""
    raise ValueError(name)


def convert_for_variant(raw: Any, variant_type: ua.VariantType, is_array: bool = False) -> Any:
    """Convert using the server-reported Variant type, with range checks."""
    if not is_array:
        return _scalar(raw, variant_type)

    values = raw
    if isinstance(values, str):
        try:
            values = json.loads(values, parse_constant=_no_constants)
        except ValueError as exc:
            raise ValueError(f"{variant_type.name} array values must be a JSON array") from exc
    if not isinstance(values, list):
        raise ValueError(f"{variant_type.name} array values must be a JSON array")
    return [_scalar(value, variant_type) for value in values]
