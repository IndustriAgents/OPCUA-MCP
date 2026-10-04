"""Namespace-zero ExtensionObjects as JSON fields, with explicit undecodable values.

Only library-known standard types are encoded. Server-defined opaque values need
DataTypeDefinition decoding; returning a debug string loses their data (#171).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opcua import ua

from .adapters.asyncua_values import standard_fields
from .contract import CONTRACT


class _Undecodable(ValueError):
    pass


def extension_to_json(value: Any, scalar: Callable) -> dict:
    """Encode a known structure's Body fields using their declared UA types."""
    max_depth = CONTRACT["limits"]["maxNestingDepth"]
    max_items = CONTRACT["limits"]["maxArrayItems"]

    def field(item: Any, type_name: str, depth: int) -> Any:
        if depth > max_depth:
            raise _Undecodable
        if item is None:
            return None
        if type_name.startswith("ListOf"):
            if len(item) > max_items:
                raise _Undecodable
            return [field(x, type_name[6:], depth + 1) for x in item]
        if type_name == "LocalizedText":
            return {"Locale": item.Locale, "Text": item.Text}
        if type_name == "QualifiedName":
            return {"NamespaceIndex": item.NamespaceIndex, "Name": item.Name}
        if type_name == "Variant":
            kind = item.VariantType.name
            if item.is_array or isinstance(item.Value, (list, tuple)):
                kind = "ListOf" + kind
            return field(item.Value, kind, depth + 1)
        if (
            type_name == "ExtensionObject"
            or hasattr(item, "ua_types")
            or standard_fields(item) is not None
        ):
            return structure(item, depth + 1)
        return scalar(item, type_name)

    def structure(item: Any, depth: int) -> dict:
        if depth > max_depth or isinstance(item, ua.ExtensionObject):
            raise _Undecodable
        # A server-defined class must not pass as a standard type by name.
        if type(item) is getattr(ua, type(item).__name__, None):
            fields = getattr(item, "ua_types", None)
        else:
            fields = standard_fields(item)
        if fields is None or len(fields) > max_items:
            raise _Undecodable
        return {name: field(getattr(item, name), kind, depth + 1) for name, kind in fields}

    try:
        return structure(value, 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"$opcua": "undecodableExtensionObject"}
