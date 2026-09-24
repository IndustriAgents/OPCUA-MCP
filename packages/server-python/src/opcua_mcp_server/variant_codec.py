"""Strict MCP JSON to OPC UA Variant conversion for writes."""

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

from .errors import message
from .limits import MAX_BYTE_STRING_BYTES, LimitExceeded

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


def _integer(raw: Any, variant_type: ua.VariantType) -> int:
    text = str(raw).strip()
    if isinstance(raw, bool) or not re.fullmatch(r"[+-]?\d+", text):
        raise ValueError(f"Cannot convert {raw!r} to {variant_type.name}")
    value = int(text)
    minimum, maximum = _INTEGER_RANGES[variant_type]
    if not minimum <= value <= maximum:
        raise ValueError(f"{text} is outside the {variant_type.name} range")
    return value


def _boolean(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Cannot convert {raw!r} to Boolean")


def _scalar(raw: Any, variant_type: ua.VariantType) -> Any:
    if raw is None:
        raise ValueError(f"Cannot write null as {variant_type.name}")
    if variant_type in _INTEGER_RANGES:
        return _integer(raw, variant_type)
    if variant_type == ua.VariantType.Boolean:
        return _boolean(raw)
    if variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Cannot convert {raw!r} to {variant_type.name}") from exc
        if not math.isfinite(value):
            raise ValueError(f"Cannot convert {raw!r} to {variant_type.name}")
        return value
    if variant_type == ua.VariantType.String:
        if not isinstance(raw, (str, int, float, bool)):
            raise ValueError("String values must be a JSON scalar")
        return str(raw)
    if variant_type == ua.VariantType.DateTime:
        if isinstance(raw, datetime):
            return raw
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{raw!r} is not a DateTime") from exc
    if variant_type == ua.VariantType.Guid:
        try:
            return UUID(str(raw))
        except ValueError as exc:
            raise ValueError(f"{raw!r} is not a Guid") from exc
    if variant_type == ua.VariantType.ByteString:
        if isinstance(raw, bytes):
            decoded = raw
        else:
            try:
                decoded = base64.b64decode(str(raw), validate=True)
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
        return ua.NodeId.from_string(str(raw))
    if variant_type == ua.VariantType.LocalizedText:
        return raw if isinstance(raw, ua.LocalizedText) else ua.LocalizedText(str(raw))
    if variant_type == ua.VariantType.QualifiedName:
        return raw if isinstance(raw, ua.QualifiedName) else ua.QualifiedName(str(raw))
    raise ValueError(f"Writes to OPC UA {variant_type.name} values are not supported safely")


def convert_for_variant(raw: Any, variant_type: ua.VariantType, is_array: bool = False) -> Any:
    """Convert using the server-reported Variant type, with range checks."""
    if not is_array:
        return _scalar(raw, variant_type)

    values = raw
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{variant_type.name} array values must be a JSON array") from exc
    if not isinstance(values, list):
        raise ValueError(f"{variant_type.name} array values must be a JSON array")
    return [_scalar(value, variant_type) for value in values]
