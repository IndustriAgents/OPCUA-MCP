"""Good *severity* is success, and a Good subcode is reported, not hidden (#157).

The shared table is ``tests/fixtures/status-severity.json``;
``packages/server-node/test/status-severity.test.mjs`` drives the same one through
the Node server, which used to accept only plain Good.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua import ua
from opcua_mcp_server.policy import ValueBound
from opcua_mcp_server.records import history_data
from opcua_mcp_server.server import _node_value_record, check_max_change

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "status-severity.json").read_text(encoding="utf-8")
)


def _status(name: str) -> ua.StatusCode:
    return ua.StatusCode(getattr(ua.StatusCodes, name))


def _double(value: float, status: str) -> ua.DataValue:
    data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    data_value.StatusCode = _status(status)
    return data_value


@pytest.mark.parametrize(
    "case", FIXTURE["statuses"], ids=[case["name"] for case in FIXTURE["statuses"]]
)
def test_severity_decides_success(case):
    status = ua.StatusCode(case["code"])
    assert status.name == case["name"]
    assert status.is_good() is case["good"]


@pytest.mark.parametrize("case", FIXTURE["reads"], ids=[case["name"] for case in FIXTURE["reads"]])
def test_a_read_record_keeps_a_good_subcode_s_value(case):
    record = _node_value_record("ns=2;i=90", _double(51.75, case["status"]))
    assert {key: record[key] for key in case["record"]} == case["record"]


@pytest.mark.parametrize(
    "case", FIXTURE["maxChange"], ids=[case["name"] for case in FIXTURE["maxChange"]]
)
def test_max_change_measures_from_a_good_subcode_s_value(case):
    arguments = (
        "ns=2;i=90",
        case["value"],
        ValueBound(max_change=case["limit"]),
        _double(case["current"], case["status"]),
    )
    if case["error"] is None:
        check_max_change(*arguments)
    else:
        with pytest.raises(ToolError) as raised:
            check_max_change(*arguments)
        assert str(raised.value) == case["error"]


@pytest.mark.parametrize(
    "case", FIXTURE["history"], ids=[case["name"] for case in FIXTURE["history"]]
)
def test_an_empty_history_range_is_empty_not_a_failure(case):
    result = SimpleNamespace(StatusCode=_status(case["status"]), HistoryData=None)
    if "error" in case:
        with pytest.raises(ValueError) as raised:
            history_data(result, "Read history", "DataValues")
        assert str(raised.value) == case["error"]
    else:
        assert history_data(result, "Read history", "DataValues") == case["records"]
