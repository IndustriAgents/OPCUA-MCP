"""What type a method argument is sent as.

Two questions, and the two runtimes used to answer both differently (#157):

* The method declares an argument's DataType, but it is not a built-in one —
  ``Duration`` (``i=290``), ``UtcTime`` (``i=294``), an enumeration. The Node
  runtime failed the call; this one could not look the type up, fell back to
  *guessing every argument*, and sent the call. Now both walk the DataType's
  supertypes to the built-in type it is encoded as — Part 3 §5.8.2: a Duration
  *is* a Double on the wire, an enumeration an Int32 — and refuse, before
  anything is sent, when there is none.
* The method declares nothing. Then a JSON boolean, number or string maps to
  Boolean, Double or String — the number as a Double because JSON, and so the
  Node runtime, cannot tell ``5`` from ``5.0`` — and a numeric string is a
  Double when it is a number by the shared grammar. A null, array or object has
  no single obvious OPC UA type, and the two runtimes had picked different ones
  (``"1,2"`` against an Int64 array), so it is refused.

``method-arguments.ts`` is the other half; ``tests/fixtures/method-arguments.json``
holds both to the same table.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from opcua import ua

from .numeric import numeric_text
from .variant_codec import convert_for_variant

#: ns=0 identifiers that *are* built-in types (Part 6 §5.1.2), numbered the same
#: as the VariantType enum.
_BUILT_IN = range(1, 26)
#: Enumeration: every enumerated DataType is encoded as an Int32 (Part 6 §5.2.4).
_ENUMERATION = 29
#: Far deeper than any real type hierarchy; bounds a server whose HasSubtype
#: references form a loop.
_MAX_DEPTH = 32

_NS0_NUMERIC = re.compile(r"ns=0;i=([0-9]+)")


def built_in_type(data_type: str, supertype_of: Callable[[str], str | None]) -> ua.VariantType:
    """The built-in type ``data_type`` is encoded as, walking up its supertypes.

    ``data_type`` is a canonical node id (``ns=0;i=290``); ``supertype_of`` gives
    the parent of one, or None. Raises ValueError when the walk ends without
    reaching a built-in type — the argument cannot be encoded, and sending a
    guess instead is exactly what this exists to stop.
    """
    current: str | None = data_type
    for _ in range(_MAX_DEPTH):
        if current is None:
            break
        match = _NS0_NUMERIC.fullmatch(current)
        if match:
            identifier = int(match.group(1))
            if identifier in _BUILT_IN:
                return ua.VariantType(identifier)
            if identifier == _ENUMERATION:
                return ua.VariantType.Int32
        current = supertype_of(current)
    raise ValueError(
        f"DataType {data_type} is not a subtype of any built-in OPC UA type, so an "
        "argument of it cannot be encoded; nothing was sent"
    )


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    return "an array" if isinstance(value, list) else "an object"


def guess_variant(value: Any, index: int) -> ua.Variant:
    """An argument for a method that declares no InputArguments."""
    if isinstance(value, bool):
        return ua.Variant(value, ua.VariantType.Boolean)
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and numeric_text(value) is not None
    ):
        return ua.Variant(convert_for_variant(value, ua.VariantType.Double), ua.VariantType.Double)
    if isinstance(value, str):
        return ua.Variant(value, ua.VariantType.String)
    raise ValueError(
        f"arguments[{index}] is {_kind(value)}, and the method publishes no "
        "InputArguments to say what type it expects; pass a boolean, a number or a string"
    )
