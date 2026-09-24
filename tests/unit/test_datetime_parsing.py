"""Unit tests for the Python server's date/time handling.

No OPC UA server and no MCP transport: this is the pure conversion layer that
sits between what MCP delivers (strings) and what the opcua client needs
(datetimes). It has broken before — the history tool originally accepted a
different shape and reported errors differently from the Node server — and the
two runtimes then disagreed about the grammar itself and about what a value with
no zone means (#157).

The rules are the shared table in ``tests/fixtures/datetime-parsing.json``;
``packages/server-node/test/dates.test.mjs`` drives the same table through the
Node server's ``toDate``.
"""

from __future__ import annotations

import json
from datetime import timezone

import pytest
from conftest import ROOT
from opcua_mcp_server import parse_iso_datetime

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "datetime-parsing.json").read_text(encoding="utf-8")
)["cases"]


def _utc_ms(value) -> str:
    """The instant as the fixture writes it: ISO-8601 UTC at millisecond precision."""
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond // 1000:03d}Z"


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_shared_table(case):
    if "error" in case:
        with pytest.raises(ValueError) as excinfo:
            parse_iso_datetime(case["input"])
        assert str(excinfo.value) == case["error"]
    else:
        parsed = parse_iso_datetime(case["input"])
        assert parsed.tzinfo is not None, "an aware datetime, never a naive one"
        assert _utc_ms(parsed) == case["expected"]
        # Truncated to milliseconds, as the Node runtime's Date holds it.
        assert parsed.microsecond % 1000 == 0


def test_none_passes_through():
    """None means 'unset' over MCP and must not become an error."""
    assert parse_iso_datetime(None) is None


def test_error_does_not_leak_the_underlying_exception():
    """No chained low-level cause for the model to see."""
    with pytest.raises(ValueError) as excinfo:
        parse_iso_datetime("nope")
    assert excinfo.value.__cause__ is None
