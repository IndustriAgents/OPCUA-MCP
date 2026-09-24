"""The one numeric-string grammar, and JSON text the way the Node runtime writes it.

A number can reach a write as a string — ``"42.5"`` — and three places read it:
the write codec, the operator policy's bounds, and the method-argument guess.
Each used to parse it with its runtime's own number parser, and the two runtimes'
parsers disagree about almost everything but plain decimals: ``float()`` takes
``"1_000"``, ``"inf"``, ``"nan"`` and Arabic-Indic digits, ``Number()`` takes
``"0x10"``, ``""`` and ``"Infinity"``. So the same string could be written to the
plant by one runtime, refused by the other, or pass a bound on one and not the
other (#157).

Now there is one grammar, JSON's own number grammar — the syntax a number would
have had if it had not been quoted — with surrounding JSON whitespace allowed.
``numeric.ts`` is the other half, and ``tests/fixtures/write-coercion.json`` and
``value-bounds.json`` pin both to the same table.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

#: JSON's number grammar (RFC 8259 §6), with ASCII digits only. ``[0-9]`` rather
#: than ``\d``, which in Python matches every Unicode decimal digit.
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_JSON_NUMBER_PARTS = re.compile(r"(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?")

#: The whitespace JSON allows around a value. Not ``str.strip()``, whose idea of
#: whitespace (NBSP, U+2028, ...) differs from JavaScript's ``trim()``.
_JSON_WHITESPACE = " \t\n\r"

#: The largest integer a JSON number carries exactly in every parser. A larger
#: one is already rounded by the time the Node runtime sees it.
MAX_SAFE_INTEGER = 2**53 - 1


def numeric_text(text: str) -> str | None:
    """``text`` without surrounding JSON whitespace, if it is a JSON number."""
    trimmed = text.strip(_JSON_WHITESPACE)
    return trimmed if _JSON_NUMBER.fullmatch(trimmed) else None


def parse_numeric_string(text: str) -> float | None:
    """A numeric string's value as a float, or None if it is not one.

    None for anything outside the grammar, and for a number too large to be a
    finite double (``"1e400"``) — there is no value to compare or write.
    """
    trimmed = numeric_text(text)
    if trimmed is None:
        return None
    value = float(trimmed)
    return value if math.isfinite(value) else None


#: Past this many decimal digits no integer type OPC UA has can hold the value,
#: so an exponent is not expanded further (``"1e999999999"`` must not allocate).
_MAX_INTEGER_DIGITS = 40


def exact_integer(text: str) -> int | str | None:
    """A JSON-number string's exact integer value.

    ``"5.0"`` and ``"1e3"`` are integers and ``"1.5"`` is not (None). Evaluated
    on the digits, not through a float, so a 64-bit value survives whole.
    ``"too large"`` when the value has more digits than any integer type holds.
    """
    match = _JSON_NUMBER_PARTS.fullmatch(text)
    if match is None:
        return None
    sign, whole, fraction, exponent = match.groups()
    digits = (whole + (fraction or "")).lstrip("0")
    if not digits:
        return 0
    scale = int(exponent or "0") - len(fraction or "")
    if scale < 0:
        if digits[scale:].strip("0"):
            return None
        digits = digits[:scale] or "0"
        scale = 0
    if len(digits) + scale > _MAX_INTEGER_DIGITS:
        return "too large"
    value = int(digits) * 10**scale
    return -value if sign else value


def js_number(value: float) -> str:
    """A float as JavaScript's ``String(number)`` writes it (ECMA-262 Number::toString).

    Both languages pick the same shortest round-tripping digits; they only lay
    them out differently — ``1e-07`` against ``1e-7``, ``1e+16`` against
    ``10000000000000000``, ``5.0`` against ``5``. Messages that echo a number
    have to be identical on both runtimes, so this is the layout they use.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    digits = whole + fraction
    point = len(whole) + int(exponent or "0")
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    count = len(digits)
    if count <= point <= 21:
        text = digits + "0" * (point - count)
    elif 0 < point <= 21:
        text = f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        text = "0." + "0" * -point + digits
    else:
        power = point - 1
        text = digits[0] + (f".{digits[1:]}" if count > 1 else "")
        text += f"e{'+' if power >= 0 else '-'}{abs(power)}"
    return sign + text


def json_text(value: Any) -> str:
    """``value`` as ``JSON.stringify`` would write it, for echoing in a message.

    An integer past 2**53 is written as the double it becomes in JavaScript,
    because that is the value the Node runtime is holding.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INTEGER:
            return str(value)
        try:
            return js_number(float(value))
        except OverflowError:
            return "null"
    if isinstance(value, float):
        return js_number(value) if math.isfinite(value) else "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        members = (
            f"{json.dumps(str(k), ensure_ascii=False)}:{json_text(v)}" for k, v in value.items()
        )
        return "{" + ",".join(members) + "}"
    return json.dumps(str(value), ensure_ascii=False)
