from __future__ import annotations

from datetime import datetime

import pytest
from opcua import ua
from opcua_mcp_server.variant_codec import convert_for_variant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(False, False), ("false", False), ("0", False), ("yes", True), (1, True)],
)
def test_boolean_conversion_is_explicit(raw, expected):
    assert convert_for_variant(raw, ua.VariantType.Boolean) is expected


def test_boolean_rejects_unknown_words():
    with pytest.raises(ValueError, match="Boolean"):
        convert_for_variant("definitely", ua.VariantType.Boolean)


def test_integer_conversion_is_integral_and_range_checked():
    assert convert_for_variant("-2147483648", ua.VariantType.Int32) == -(2**31)
    with pytest.raises(ValueError, match="Int32"):
        convert_for_variant("1.5", ua.VariantType.Int32)
    with pytest.raises(ValueError, match="range"):
        convert_for_variant(str(2**31), ua.VariantType.Int32)


def test_uint64_does_not_lose_precision():
    assert convert_for_variant(str(2**64 - 1), ua.VariantType.UInt64) == 2**64 - 1


def test_bytestring_is_base64_not_utf8_text():
    assert convert_for_variant("AP8=", ua.VariantType.ByteString) == b"\x00\xff"
    with pytest.raises(ValueError, match="base64"):
        convert_for_variant("not base64!", ua.VariantType.ByteString)


def test_array_accepts_json_and_converts_every_item():
    assert convert_for_variant('["1", "2"]', ua.VariantType.Int16, True) == [1, 2]


def test_datetime_is_parsed_as_a_datetime():
    value = convert_for_variant("2026-09-17T10:30:00Z", ua.VariantType.DateTime)
    assert isinstance(value, datetime)
    assert value.isoformat() == "2026-09-17T10:30:00+00:00"
