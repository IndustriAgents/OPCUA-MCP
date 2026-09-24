"""How a server's OperationLimits combine with this project's own (issue #139).

``packages/server-node/test/operation-limits.test.mjs`` reads the same
``tests/fixtures/operation-limits.json``. The rules are small and the reason for
a shared table is the usual one: a server limit one runtime honours and the
other does not is one plant request refused by one client and not the other.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.limits import aggregate_intervals, chunked, effective_limit
from opcua_mcp_server.operation_limits import (
    UNSTATED,
    read_chunk,
    read_operation_limits,
    write_limit,
)

TABLE = json.loads(
    (ROOT / "tests" / "fixtures" / "operation-limits.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case", TABLE["effective"], ids=[case["name"] for case in TABLE["effective"]]
)
def test_a_server_limit_can_only_lower_ours(case):
    assert effective_limit(case["project"], case["server"]) == case["effective"]


@pytest.mark.parametrize("case", TABLE["chunks"], ids=[case["name"] for case in TABLE["chunks"]])
def test_a_read_is_split_in_order_as_the_shared_table_says(case):
    items = list(range(case["count"]))
    parts = chunked(items, case["size"])
    assert [len(part) for part in parts] == case["requests"]
    # In order, with nothing lost or repeated: the records are put back by
    # position, so a chunking that reordered would misattribute every value.
    assert [item for part in parts for item in part] == items


@pytest.mark.parametrize(
    "case",
    TABLE["aggregateIntervals"],
    ids=[case["name"] for case in TABLE["aggregateIntervals"]],
)
def test_aggregate_intervals_count_as_the_shared_table_says(case):
    assert (
        aggregate_intervals(case["start_ms"], case["end_ms"], case["interval_ms"]) == case["count"]
    )


@pytest.mark.parametrize(
    ("name", "identifier"),
    [
        ("maxNodesPerRead", ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead),
        (
            "maxNodesPerWrite",
            ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerWrite,
        ),
        (
            "maxNodesPerBrowse",
            ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse,
        ),
        (
            "maxNodesPerTranslateBrowsePathsToNodeIds",
            ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerTranslateBrowsePathsToNodeIds,
        ),
    ],
)
def test_the_contract_names_the_spec_nodes(name, identifier):
    """The node ids are the spec's, as the client library numbers them."""
    assert CONTRACT["operationLimits"][name] == f"ns=0;i={identifier}"


class _Client:
    """Just enough of a python-opcua client to answer one batched Read."""

    def __init__(self, values):
        self.uaclient = SimpleNamespace(get_attributes=lambda nodes, attribute: values)


def _data_value(value, good=True):
    status = ua.StatusCode(ua.StatusCodes.Good if good else ua.StatusCodes.BadNodeIdUnknown)
    return SimpleNamespace(StatusCode=status, Value=SimpleNamespace(Value=value))


def test_what_the_server_states_is_read_and_what_it_does_not_is_none():
    limits = read_operation_limits(
        _Client([_data_value(100), _data_value(50), _data_value(None, good=False), _data_value(0)])
    )
    assert limits == {
        "maxNodesPerRead": 100,
        "maxNodesPerWrite": 50,
        "maxNodesPerBrowse": None,
        "maxNodesPerTranslateBrowsePathsToNodeIds": 0,
    }
    assert read_chunk(limits) == 100
    assert write_limit(limits) == 50


def test_a_server_that_cannot_be_asked_states_no_limit():
    class Broken:
        class uaclient:
            @staticmethod
            def get_attributes(nodes, attribute):
                raise ConnectionError("gone")

    assert read_operation_limits(Broken()) == UNSTATED
    assert read_chunk(UNSTATED) == CONTRACT["limits"]["maxNodesPerRead"]
    assert write_limit(UNSTATED) == CONTRACT["limits"]["maxNodesPerWrite"]
