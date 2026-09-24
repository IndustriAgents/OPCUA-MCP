"""End-to-end tests for aggregate history reads on both MCP servers.

These run against the aggregate-capable mock (`packages/mock-server-aggregate`,
:4841) rather than the main mock, which deliberately advertises no aggregate
functions.

Since the history and aggregate tools merged, the capability gates an *argument*
rather than a tool: `read_opcua_history` is offered wherever the server reports
HistoricalAccess, and `aggregate_function` appears on it only where the server
also advertises aggregates — with that server's own function list named in the
description. The negative case, where the argument is withheld, is asserted
against the main mock in ``test_contract_parity.py``.

The mock ramps its Temperature node at a known rate, so aggregate output is
checked arithmetically rather than merely for non-emptiness: with a +1.0/second
ramp, consecutive Average buckets must differ by exactly the processing interval
expressed in seconds.

Run:
    cd tests && uv run pytest -v e2e/test_aggregate_e2e.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest
from conftest import AGGREGATE_NODE_ID, AGGREGATE_RAMP_PER_SECOND
from test_contract_parity import assert_matches_result_shape
from test_mcp_e2e import (
    ISO_UTC_TIMESTAMP,
    NODE_BUILD,
    _server_params,
    connect,
    records_of,
    text_of,
    tool_names,
)

AGGREGATE_TOOL = "read_opcua_history"

# A deliberately non-UTC zone for the regression test below. UTC+5:30 is chosen
# because it is not a whole number of hours, so a naive-local-time bug cannot be
# mistaken for an off-by-one-hour DST artefact.
NON_UTC_TZ = "Asia/Kolkata"


@pytest.fixture(params=["python", "node"])
def agg_server(request, aggregate_opcua_server):
    """``(impl_name, StdioServerParameters)`` pointed at the aggregate mock."""
    impl = request.param
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip(
            "Node server not built — run `npm install && npm run build` in packages/server-node"
        )
    return impl, _server_params(impl, aggregate_opcua_server)


# --- helpers -------------------------------------------------------------------


def iso_utc(offset_seconds: int = 0) -> str:
    """An ISO-8601 UTC timestamp, optionally offset into the past."""
    moment = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return moment.isoformat().replace("+00:00", "Z")


def aggregate_values(result) -> list[float | None]:
    """The per-bucket values of an aggregate result, ``None`` for an empty bucket.

    Both servers emit the contract's ``historyRecords`` shape, so this needs no
    per-implementation branch — it used to, because the Node server returned raw
    node-opcua ``DataValue`` JSON and stringified nothing while the Python server
    reported an empty bucket as the string ``"None"`` (issue #23).
    """
    return [record["value"] for record in records_of(result)]


async def read_average(session, *, window_seconds, interval_ms, end_time="explicit"):
    """Read Average over the last ``window_seconds``, bucketed by ``interval_ms``."""
    arguments = {
        "node_id": AGGREGATE_NODE_ID,
        "start_time": iso_utc(window_seconds),
        "aggregate_function": "Average",
        "processing_interval": interval_ms,
    }
    if end_time == "explicit":
        arguments["end_time"] = iso_utc()
    result = await session.call_tool(AGGREGATE_TOOL, arguments)
    return result, aggregate_values(result)


# --- tests ---------------------------------------------------------------------


async def test_aggregate_tool_exposed_when_supported(agg_server):
    """The mock advertises aggregate functions, so both servers must expose the tool."""
    impl, params = agg_server
    async with connect(params) as session:
        names = await tool_names(session)
    assert AGGREGATE_TOOL in names, f"{impl}: aggregate tool missing despite server support"


async def test_aggregate_average_values_are_correct(agg_server):
    """Averages over a known linear ramp must advance by the interval, in seconds.

    This is the check that actually exercises `ReadProcessedDetails`: a tool that
    returned records of the wrong node, wrong window, or unaggregated raw history
    would not produce this exact progression.
    """
    impl, params = agg_server
    interval_ms = 5000
    async with connect(params) as session:
        result, values = await read_average(session, window_seconds=20, interval_ms=interval_ms)

    assert not result.is_error, text_of(result)
    records = records_of(result)
    # The aggregate tool shares the history tool's record shape (contract ->
    # resultShapes.historyRecords); both servers must produce it.
    assert_matches_result_shape(records, "historyRecords", f"{impl}/{AGGREGATE_TOOL}")
    assert all(ISO_UTC_TIMESTAMP.match(r["timestamp"]) for r in records), (
        f"{impl}: bucket timestamps are not ISO-8601 UTC: {[r['timestamp'] for r in records]}"
    )

    populated = [v for v in values if v is not None]
    assert len(populated) >= 3, f"{impl}: too few populated buckets: {values}"

    # Drop the final bucket: it can be clipped by the end of the window and so
    # average over a shorter span than the rest.
    expected_delta = AGGREGATE_RAMP_PER_SECOND * interval_ms / 1000
    deltas = [round(b - a, 3) for a, b in pairwise(populated)][:-1]
    assert deltas, f"{impl}: not enough buckets to compare: {values}"
    assert all(abs(d - expected_delta) < 0.75 for d in deltas), (
        f"{impl}: expected ~{expected_delta} per {interval_ms}ms bucket, got {deltas}"
    )


async def test_aggregate_default_end_time_is_utc(agg_server):
    """Omitting `end_time` must not overshoot the window on a non-UTC host.

    Regression test for #24: the Python server defaulted `EndTime` to a naive
    local `datetime.now()` while parsing `start_time` as aware UTC, so on a host
    at UTC+X the window ran X hours into the future and the response was padded
    with one empty bucket per processing interval in between.

    The server is launched under an explicit non-UTC ``TZ`` so this fails on UTC
    CI runners too, where the bug would otherwise be invisible.
    """
    impl, params = agg_server
    window_seconds, interval_ms = 60, 10_000
    skewed = params.model_copy(update={"env": {**(params.env or {}), "TZ": NON_UTC_TZ}})

    async with connect(skewed) as session:
        result, values = await read_average(
            session,
            window_seconds=window_seconds,
            interval_ms=interval_ms,
            end_time="default",
        )

    assert not result.is_error, text_of(result)
    # A 60s window in 10s buckets is 6 buckets, plus at most one for the boundary.
    # Under the bug this was 727 on a UTC+2 host and would be ~1986 at UTC+5:30.
    max_buckets = window_seconds * 1000 // interval_ms + 2
    assert len(values) <= max_buckets, (
        f"{impl}: default end_time produced {len(values)} buckets for a "
        f"{window_seconds}s window (expected <= {max_buckets}) — "
        f"EndTime is likely naive local time rather than UTC"
    )


async def test_aggregate_rejects_unknown_function(agg_server):
    """An unsupported aggregate name is rejected, listing what the server offers."""
    impl, params = agg_server
    async with connect(params) as session:
        result = await session.call_tool(
            AGGREGATE_TOOL,
            {
                "node_id": AGGREGATE_NODE_ID,
                "start_time": iso_utc(60),
                "aggregate_function": "NotAnAggregate",
                "processing_interval": 10_000,
            },
        )
    text = text_of(result)
    assert "Invalid aggregate function" in text, f"{impl}: unexpected error: {text}"
    assert "Average" in text, f"{impl}: supported functions not listed: {text}"
    assert result.is_error is True, f"{impl}: expected an error result"


async def test_an_aggregate_over_too_many_intervals_is_refused_identically(aggregate_opcua_server):
    """A millisecond interval over an hour is 3.6 million records: refused, not sent.

    The number of intervals is what the server would have to compute and send
    back, so it is held to the same cap as a raw read (issue #139). Both runtimes
    must say so in the same words, before the request reaches the server.
    """
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    arguments = {
        "node_id": AGGREGATE_NODE_ID,
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T01:00:00Z",
        "aggregate_function": "Average",
        "processing_interval": 1,
    }
    refusals = {}
    for impl in ("python", "node"):
        async with connect(_server_params(impl, aggregate_opcua_server)) as session:
            result = await session.call_tool(AGGREGATE_TOOL, arguments)
        assert result.is_error, f"{impl}: 3.6 million intervals were asked for"
        refusals[impl] = text_of(result)
    assert (
        refusals["python"]
        == refusals["node"]
        == (
            "read_opcua_history would ask the OPC UA server for 3600000 intervals of 1 ms over "
            "that range, over the 5000-interval limit on one aggregate read "
            "(limits.maxHistoryValues). Nothing was sent. Widen processing_interval or narrow "
            "start_time/end_time."
        )
    ), refusals
