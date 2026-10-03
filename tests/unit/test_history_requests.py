"""Raw reads and release requests use the same contract-owned details (#172)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from conftest import ROOT
from opcua_mcp_server.history import raw_details

CASES = json.loads((ROOT / "tests/fixtures/history-requests.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_raw_history_request_has_no_unrequested_bounds(case):
    start = datetime.fromisoformat(case["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(case["end"].replace("Z", "+00:00"))
    request = raw_details(start, end, case["count"])
    assert request.ReturnBounds is case["returnBounds"]
    assert request.IsReadModified is False
    assert request.NumValuesPerNode == case["count"]
    assert request.StartTime == start
    assert request.EndTime == end
