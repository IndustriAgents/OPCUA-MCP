"""Native aggregate pages are consumed completely or fail, identically in both runtimes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.errors import message
from opcua_mcp_server.history import aggregate_pages, read_continuation, release_continuation_point

CASES = json.loads((ROOT / "tests/fixtures/aggregate-pages.json").read_text(encoding="utf-8"))[
    "cases"
]


def result(page):
    return SimpleNamespace(
        ContinuationPoint=page.get("point", "").encode(),
        StatusCode=ua.StatusCode(getattr(ua.StatusCodes, page.get("status", "Good"))),
        HistoryData=SimpleNamespace(DataValues=page["values"]) if "values" in page else None,
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_aggregate_pages(case):
    reads, released = [], []
    pending = iter(case["pages"][1:])

    def read(point):
        reads.append(point.decode())
        page = next(pending)
        if "throw" in page:
            raise ConnectionError(page["throw"])
        return result(page)

    def release(point):
        released.append(point.decode())
        if case.get("releaseThrows"):
            raise ConnectionError("release failed")

    def run():
        return aggregate_pages(result(case["pages"][0]), read, release, case.get("limit", 5000))

    if "expected" in case:
        assert run() == case["expected"]
    else:
        expected = case.get("errorText") or message(case["error"], limit=case.get("limit", 5000))
        with pytest.raises((ValueError, ConnectionError)) as error:
            run()
        assert str(error.value) == expected
    assert reads == case["reads"]
    assert released == case["released"]


def test_resume_and_release_keep_original_query_and_session():
    requests = []
    details = ua.ReadProcessedDetails()
    node = SimpleNamespace(nodeid=ua.NodeId(7, 2))
    response = result({"values": [1]})

    def send(params):
        requests.append(params)
        return [response]

    client = SimpleNamespace(get_node=lambda _: node, uaclient=SimpleNamespace(history_read=send))
    assert read_continuation(client, "ns=2;i=7", b"held", details) is response
    release_continuation_point(client, "ns=2;i=7", b"held", details)
    for index, params in enumerate(requests):
        assert params.HistoryReadDetails is details
        assert params.NodesToRead[0].NodeId == node.nodeid
        assert params.NodesToRead[0].ContinuationPoint == b"held"
        assert params.TimestampsToReturn == ua.TimestampsToReturn.Both
        assert params.ReleaseContinuationPoints is bool(index)


def test_cancellation_releases_held_point():
    released = []

    def cancel(_):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        aggregate_pages(result({"values": [1], "point": "a"}), cancel, released.append)
    assert released == [b"a"]


async def test_history_tool_reports_completed_server_pages():
    from opcua_mcp_server.server import read_opcua_history

    queries = []
    node_id = ua.NodeId(7, 2)

    def page(value, point):
        item = result({"values": [ua.DataValue(ua.Variant(value, ua.VariantType.Double))]})
        item.ContinuationPoint = point
        return item

    def initial(details):
        queries.append(details)
        return page(1, b"held")

    def resume(params):
        assert params.HistoryReadDetails is queries[0]
        assert params.NodesToRead[0].ContinuationPoint == b"held"
        assert params.ReleaseContinuationPoints is False
        return [page(2, None)]

    node = SimpleNamespace(nodeid=node_id, history_read=initial)
    client = SimpleNamespace(get_node=lambda _: node, uaclient=SimpleNamespace(history_read=resume))
    state = SimpleNamespace(
        capabilities=SimpleNamespace(aggregate_functions={"Average": ua.NodeId(2342)})
    )
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"opcua_client": client, "state": state})
    )
    answer = await read_opcua_history(
        "ns=2;i=7",
        ctx,
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:02:00Z",
        aggregate_function="Average",
        processing_interval=60000,
    )
    assert [record["value"] for record in answer.structured_content["result"]] == [1, 2]
    assert answer.structured_content["completeness"]["complete"] is True
    assert answer.structured_content["completeness"]["continuation"] is None
