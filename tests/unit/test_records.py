"""Unit tests for the canonical history-family record shape (Python side).

`contract/tools.json` -> `resultShapes.historyRecords` defines one shape for
`read_history_opcua_node` / `read_aggregate_opcua_node` on both servers; this
file pins the Python implementation of it. The Node equivalent is the
"history records" suite in packages/server-node/test/unit.test.mjs.

The per-type value encoding is not restated here — it lives in
`tests/fixtures/value-encoding.json`, which both suites read. Each side builds
the *native* value for a case (that is the whole problem: `bytes` here, a
`Buffer` there) and asserts the same JSON comes out. A case with no native value
below fails rather than silently going unchecked, so adding one to the fixture
forces both runtimes to handle it.

No OPC UA server and no MCP transport.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server import format_iso_utc, history_record, history_records, variant_to_json

TIMESTAMP = datetime(2026, 9, 9, 13, 36, 1, 468000)

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "value-encoding.json").read_text())
CASES = {case["name"]: case for case in FIXTURE["cases"]}

# The native python-opcua value for each case in the fixture. The Node suite has
# its own table of the same names holding node-opcua values; the two produce the
# same JSON, which is the point.
NATIVE = {
    "boolean": ua.Variant(True, ua.VariantType.Boolean),
    "int32": ua.Variant(42, ua.VariantType.Int32),
    "int64_small": ua.Variant(5, ua.VariantType.Int64),
    "int64_negative": ua.Variant(-5, ua.VariantType.Int64),
    "int64_beyond_double": ua.Variant(2**53 + 1, ua.VariantType.Int64),
    "uint64_max": ua.Variant(2**64 - 1, ua.VariantType.UInt64),
    "double": ua.Variant(51.75, ua.VariantType.Double),
    "double_nan": ua.Variant(float("nan"), ua.VariantType.Double),
    "double_infinity": ua.Variant(float("inf"), ua.VariantType.Double),
    "string": ua.Variant("AUTO", ua.VariantType.String),
    "datetime": ua.Variant(TIMESTAMP, ua.VariantType.DateTime),
    "guid": ua.Variant(uuid.UUID("72962B91-FA75-4AE6-8D28-B404DC7DAF63"), ua.VariantType.Guid),
    "bytestring": ua.Variant(b"abc", ua.VariantType.ByteString),
    "nodeid": ua.Variant(ua.NodeId(3, 2), ua.VariantType.NodeId),
    "nodeid_namespace_zero": ua.Variant(ua.NodeId(2253), ua.VariantType.NodeId),
    "statuscode": ua.Variant(ua.StatusCode(0), ua.VariantType.StatusCode),
    "qualifiedname": ua.Variant(ua.QualifiedName("Temperature", 2), ua.VariantType.QualifiedName),
    "localizedtext": ua.Variant(
        ua.LocalizedText("Ambient temperature"), ua.VariantType.LocalizedText
    ),
    "double_array": ua.Variant([1.5, 2.5], ua.VariantType.Double),
    "byte_array": ua.Variant([97, 98, 99], ua.VariantType.Byte),
    "int64_array": ua.Variant([5, 2**53 + 1], ua.VariantType.Int64),
    "bytestring_array": ua.Variant([b"ab", b"c"], ua.VariantType.ByteString),
    "empty_array": ua.Variant([], ua.VariantType.Double),
    "null": ua.Variant(None, ua.VariantType.Null),
}


def data_value(value, *, timestamp=TIMESTAMP, status="Good"):
    """A stand-in for an opcua `DataValue` — the attributes the mapper reads."""
    return SimpleNamespace(
        Value=value if isinstance(value, ua.Variant) else SimpleNamespace(Value=value),
        SourceTimestamp=timestamp,
        StatusCode=None if status is None else SimpleNamespace(name=status),
    )


# --- the shared value-encoding table ------------------------------------------


def test_every_fixture_case_has_a_native_value():
    """A case with no entry above would pass by never being run."""
    assert set(NATIVE) == set(CASES), (
        f"missing native values for {sorted(set(CASES) - set(NATIVE))}; "
        f"unknown cases {sorted(set(NATIVE) - set(CASES))}"
    )


@pytest.mark.parametrize("name", sorted(CASES), ids=sorted(CASES))
def test_value_encoding_matches_the_shared_fixture(name):
    """The Node suite asserts the same expectations against node-opcua values."""
    case = CASES[name]
    encoded = variant_to_json(NATIVE[name])

    if "expectedPattern" in case:
        assert isinstance(encoded, str) and re.match(case["expectedPattern"], encoded), (
            f"{name}: {encoded!r} does not match {case['expectedPattern']!r}"
        )
    else:
        assert encoded == case["expected"], f"{name}: {encoded!r} != {case['expected']!r}"


@pytest.mark.parametrize("name", ["boolean", "int32", "double", "string"], ids=str)
def test_json_native_values_are_not_stringified(name):
    """The value used to be `str(...)`, so a number arrived as `"51.25"`."""
    assert not isinstance(variant_to_json(NATIVE[name]), str) or name == "string"


def test_bytestring_is_not_the_python_repr_of_bytes():
    """Regression guard: `str(b"abc")` is `"b'abc'"`, which no other runtime emits."""
    assert variant_to_json(NATIVE["bytestring"]) == "YWJj"


def test_arrays_are_encoded_element_wise():
    """One oversized Int64 element must not stringify its neighbours."""
    assert variant_to_json(NATIVE["int64_array"]) == [5, "9007199254740993"]


# --- the record ----------------------------------------------------------------


def test_flattens_a_data_value():
    assert history_records([data_value(ua.Variant(51.25, ua.VariantType.Double))]) == [
        {"value": 51.25, "timestamp": "2026-09-09T13:36:01.468000Z", "status": "Good"}
    ]


def test_empty_aggregate_interval_is_null_not_the_string_none():
    """An interval the server has no data for previously came back as `"None"`."""
    record = history_record(data_value(None, status="BadNoData"))
    assert record["value"] is None
    assert record["status"] == "BadNoData"


def test_absent_status_code_means_good():
    assert history_record(data_value(1, status=None))["status"] == "Good"


def test_no_data_values_is_an_empty_record_list():
    assert history_records(None) == []
    assert history_records([]) == []


# --- format_iso_utc -----------------------------------------------------------


def test_naive_timestamps_are_labelled_utc_not_reinterpreted_as_local():
    """python-opcua decodes OPC UA DateTimes to naive datetimes already in UTC."""
    assert format_iso_utc(TIMESTAMP) == "2026-09-09T13:36:01.468000Z"


def test_aware_timestamps_are_converted_to_utc():
    kolkata = timezone(timedelta(hours=5, minutes=30))
    assert format_iso_utc(TIMESTAMP.replace(tzinfo=kolkata)) == "2026-09-09T08:06:01.468000Z"


def test_none_passes_through():
    assert format_iso_utc(None) is None


def test_round_trips_through_the_parser_the_tools_accept():
    """The tools accept ISO-8601 for start_time/end_time; they now return it too."""
    from opcua_mcp_server import parse_iso_datetime

    assert parse_iso_datetime(format_iso_utc(TIMESTAMP)) == TIMESTAMP.replace(tzinfo=timezone.utc)
