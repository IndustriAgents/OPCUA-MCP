"""Maintained-library values at the existing internal UA request boundary.

Only locally constructed legacy request DTOs are serialized here. Network
responses are parsed by asyncua and never decoded by the legacy library.
"""

from __future__ import annotations

import re
from dataclasses import fields, is_dataclass
from enum import Enum

from asyncua import ua
from asyncua.common.utils import Buffer
from asyncua.ua import ua_binary, uaprotocol_auto
from opcua import ua as legacy
from opcua.ua import ua_binary as legacy_binary


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
    """Convert a local request DTO to asyncua without maintaining a second UA field map."""
    if isinstance(value, (list, tuple)):
        return [native_request(item) for item in value]
    cls = type(value)
    if cls is getattr(ua, cls.__name__, None):
        return value
    if cls is getattr(legacy, cls.__name__, None):
        target = getattr(ua, cls.__name__, None)
        if target is None:
            raise TypeError(f"Unsupported internal UA request type: {cls.__name__}")
        if isinstance(value, Enum):
            return target(value.value)
        encoded = legacy_binary.to_binary(cls.__name__, value)
        return ua_binary.from_binary(target, Buffer(encoded))
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return value
    raise TypeError(f"Unsupported internal UA request type: {cls.__name__}")
