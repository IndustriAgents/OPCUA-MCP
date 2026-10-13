"""JSON decoding combines UTF-16 pairs; remaining surrogates cannot encode as UTF-8."""

from __future__ import annotations


def has_unpaired_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)
