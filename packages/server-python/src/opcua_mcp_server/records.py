"""The canonical record shape for the history family of tools.

``contract/tools.json`` -> ``resultShapes.historyRecords`` is the specification;
this module is the Python implementation of it, and ``src/records.ts`` in the
Node server is the other. Both servers must emit the same record for the same
reading: a client — or a model — that has learned one server's output has to be
able to read the other's. See issue #23.

Conversion keys on the OPC UA *data type*, not on the Python runtime type. That
is not incidental. The two client libraries represent the same OPC UA value with
wildly different native types — a ByteString is ``bytes`` here and a ``Buffer``
in Node, an Int64 is a plain ``int`` here and a ``[high, low]`` pair in Node — so
anything reaching for ``str(value)`` as its primary signal diverges by
construction. The per-type table is shared, and pinned from both sides by
``tests/fixtures/value-encoding.json``.
"""

from __future__ import annotations

import uuid
from base64 import b64encode
from collections.abc import Iterable
from datetime import datetime
from math import isfinite
from typing import Any

from .datetimes import format_iso_utc
from .node_ids import canonical_node_id

#: An absent StatusCode means Good in OPC UA, so name it rather than return None.
_DEFAULT_STATUS = "Good"

#: Past this, a JSON number silently loses digits in any JavaScript parser.
_MAX_SAFE_INTEGER = 2**53 - 1


def _int_to_json(value: int) -> int | str:
    """A 64-bit integer as a JSON number, or a decimal string when it cannot be one.

    Python integers are arbitrary precision, so an Int64 counter round-trips
    here but would be rounded by the receiving JavaScript. Both servers switch to
    a string at the same threshold, so neither rounds behind the caller's back.
    """
    return value if -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER else str(value)


def _float_to_json(value: float) -> float | str:
    """A float as JSON, with the non-finite values spelled as OPC UA spells them.

    JSON has no NaN or infinity: ``json.dumps`` emits a bare ``NaN``, which
    strict parsers reject. The string form has to be the *same* string on both
    servers, and Python's ``str(float("nan"))`` is ``"nan"`` where JavaScript's
    is ``"NaN"`` — so the spelling from OPC UA Part 6's JSON encoding is used
    explicitly rather than left to either language's formatter.
    """
    if isfinite(value):
        return value
    if value != value:  # NaN is the only value not equal to itself.
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def _bytes_to_json(value: bytes | bytearray | memoryview) -> str:
    """A ByteString as base64, the encoding OPC UA Part 6 gives it in JSON."""
    return b64encode(bytes(value)).decode("ascii")


def _node_id_to_json(value: Any) -> str:
    """A NodeId as its canonical text form, namespace always spelled out.

    Two divergences from the Node server in one place. ``str(NodeId)`` is a Python
    repr — ``"NumericNodeId(ns=2;i=3)"`` — where node-opcua gives ``"ns=2;i=3"``,
    so the ``to_string`` form is used instead. And python-opcua *omits* a zero
    namespace from that form while node-opcua writes it, so ``ns=0;`` is put back:
    an event's EventType and SourceNode are almost always in namespace 0, and the
    two servers would otherwise report ``"i=2253"`` and ``"ns=0;i=2253"`` for the
    same node.

    The restoring itself is :func:`canonical_node_id`, shared with the policy
    layer. It used to be written out here, which meant the *records* agreed on a
    spelling while the allowlist compared raw strings and did not — so the same
    node could be reported one way and authorised another.
    """
    text = value.to_string() if hasattr(value, "to_string") else str(value)
    return canonical_node_id(text)


def scalar_to_json(value: Any, type_name: str = "") -> Any:
    """One scalar OPC UA value as JSON, given its variant type name."""
    if value is None:
        return None

    # Types whose native representation differs between the two client libraries.
    if type_name in ("Int64", "UInt64"):
        return _int_to_json(int(value))
    if type_name == "ByteString":
        if isinstance(value, (bytes, bytearray, memoryview)):
            return _bytes_to_json(value)
        return str(value)
    if type_name == "DateTime":
        return format_iso_utc(value) if isinstance(value, datetime) else str(value)
    if type_name == "Guid":
        # Lower case: the RFC 4122 canonical form. node-opcua renders GUIDs upper.
        return str(value).lower()
    if type_name == "StatusCode":
        return str(getattr(value, "name", value))
    if type_name == "LocalizedText":
        # The text only; the locale is not part of the reading.
        return getattr(value, "Text", None) or ""
    if type_name in ("NodeId", "ExpandedNodeId"):
        return _node_id_to_json(value)
    if type_name == "QualifiedName":
        return f"{value.NamespaceIndex}:{value.Name}"

    # `bool` first: it is a subclass of `int` in Python, and must stay a boolean.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _int_to_json(value)
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return _float_to_json(value)

    # Fallbacks for a value that arrived without a usable variant type.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bytes_to_json(value)
    if isinstance(value, datetime):
        return format_iso_utc(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [scalar_to_json(item, type_name) for item in value]

    # Structured and opaque types (ExtensionObject, XmlElement, …) degrade to a
    # string. Those are not the shape of anything a server historises as a
    # variable value, and a faithful cross-runtime encoding of them would be a
    # much larger undertaking than this record is worth.
    return str(value)


def variant_to_json(variant: Any) -> Any:
    """An OPC UA value as JSON, without the Variant wrapper.

    Values that JSON can carry pass through as themselves, so a consumer gets
    ``51.75`` rather than ``"51.75"`` — and an empty aggregate interval gets
    ``null`` rather than the string ``"None"``. Everything else follows the
    per-type table in :func:`scalar_to_json`.
    """
    if variant is None:
        return None

    value = getattr(variant, "Value", None)
    if value is None:
        return None

    type_name = getattr(getattr(variant, "VariantType", None), "name", "")

    # Array-ness comes from the variant where it says so. Unlike Node, duck-typing
    # is a safe fallback here: no scalar python-opcua value is a list.
    if getattr(variant, "is_array", False) or isinstance(value, (list, tuple)):
        return [scalar_to_json(item, type_name) for item in value]

    return scalar_to_json(value, type_name)


def history_record(data_value: Any) -> dict:
    """One opcua ``DataValue`` as a canonical record."""
    status = getattr(data_value, "StatusCode", None)
    return {
        "value": variant_to_json(getattr(data_value, "Value", None)),
        "timestamp": format_iso_utc(getattr(data_value, "SourceTimestamp", None)),
        "status": str(status.name) if status is not None else _DEFAULT_STATUS,
    }


def history_records(data_values: Iterable[Any] | None) -> list[dict]:
    """A history/aggregate result as canonical records."""
    return [history_record(data_value) for data_value in data_values or ()]


def history_data(result: Any, what: str, field: str) -> list:
    """The readings (or events) a HistoryReadResult carries; [] when it has none.

    Good *severity* is success, not plain Good only. ``GoodNoData`` is how a Part
    11 server answers a range with nothing in it — an empty answer, not a failed
    read — and it may come with no HistoryData at all, which used to surface
    here as an AttributeError on ``None`` and on the Node runtime as a refusal
    (#157). ``historyData`` in ``records.ts`` is the other half.
    """
    status = result.StatusCode
    if not status.is_good():
        raise ValueError(f"{what} failed with status: {status.name}")
    data = getattr(result, "HistoryData", None)
    return list(getattr(data, field, None) or []) if data is not None else []
