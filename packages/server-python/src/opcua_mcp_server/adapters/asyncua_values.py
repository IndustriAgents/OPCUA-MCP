"""Maintained-library values at the existing internal UA request boundary.

Only locally constructed legacy request DTOs are converted here. The maintained
library owns binary encoding and parsing; legacy DTOs provide a rollback boundary.
"""

from __future__ import annotations

import re
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from asyncua import ua
from asyncua.ua import uaprotocol_auto
from opcua import ua as legacy


def standard_fields(value):
    """Descriptors of a library-known standard structure, without evaluating annotations."""
    cls = type(value)
    if (
        cls not in (getattr(ua, cls.__name__, None), getattr(uaprotocol_auto, cls.__name__, None))
        or cls.__module__
        not in {
            "asyncua.ua.uaprotocol_auto",
            "asyncua.ua.uaprotocol_hand",
            "asyncua.ua.uatypes",
        }
        or not is_dataclass(value)
        or isinstance(value, ua.ExtensionObject)
    ):
        return None
    result = []
    for field in fields(value):
        annotation = field.type
        if isinstance(annotation, type):
            if annotation is not getattr(ua, annotation.__name__, None):
                return None
            kind = annotation.__name__
        else:
            match = re.fullmatch(
                r"(list\[)?ua\.([A-Za-z][A-Za-z0-9_]*)(\])?", annotation.strip("'\"")
            )
            if not match or bool(match[1]) != bool(match[3]):
                return None
            kind = match[2]
            if getattr(ua, kind, None) is None:
                return None
            if match[1]:
                kind = "ListOf" + kind
        result.append((field.name, kind))
    return result


def native_request(value):
    """Convert local DTO fields into maintained native types without legacy binary parsing."""
    return _native_request(value, 0)


def _native_request(value, depth):
    # Internal UA envelopes add nesting beyond the independently bounded JSON
    # arguments. Refuse cycles without reaching the interpreter recursion limit.
    if depth > 32:
        raise ValueError("Internal UA request exceeds the DTO nesting limit")
    if isinstance(value, (list, tuple)):
        return [_native_request(item, depth + 1) for item in value]
    cls = type(value)
    if cls in (getattr(ua, cls.__name__, None), getattr(uaprotocol_auto, cls.__name__, None)):
        return value
    if cls is getattr(legacy, cls.__name__, None):
        if isinstance(value, legacy.NodeId):
            kind = ua.NodeIdType(value.NodeIdType.value)
            if value.NamespaceUri or value.ServerIndex:
                return ua.ExpandedNodeId(
                    value.Identifier,
                    value.NamespaceIndex,
                    kind,
                    value.NamespaceUri,
                    value.ServerIndex,
                )
            return ua.NodeId(value.Identifier, value.NamespaceIndex, kind)
        target = getattr(ua, cls.__name__, None)
        if target is None:
            raise TypeError(f"Unsupported internal UA request type: {cls.__name__}")
        if isinstance(value, Enum):
            return target(value.value)
        if isinstance(value, legacy.Variant):
            return ua.Variant(
                _native_request(value.Value, depth + 1),
                ua.VariantType(value.VariantType.value),
                value.Dimensions,
                is_array=value.is_array,
            )
        if is_dataclass(target) and hasattr(value, "ua_types"):
            names = {field.name for field in fields(target) if field.init}
            return target(
                **{
                    name: _native_request(getattr(value, name), depth + 1)
                    for name, _kind in value.ua_types
                    if name in names
                }
            )
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return value
    # DateTime and Guid are native Python values in both libraries.
    if isinstance(value, (datetime, UUID)):
        return value
    raise TypeError(f"Unsupported internal UA request type: {cls.__name__}")
