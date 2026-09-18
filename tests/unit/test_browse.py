"""Unit tests for the browse continuation-point drain (Python side).

A server may cap how many references one browse response carries and answer the
rest behind a continuation point. Taking only the first ``BrowseResult`` returns
a truncated child list *as a success*, which on a plant server with a wide node
is a wrong answer delivered confidently.

The Node equivalent is the "browse continuation points" suite in
packages/server-node/test/unit.test.mjs, and the two are deliberately the same
cases: this is exactly the kind of behaviour that drifts when only one runtime
is pinned.

There is no end-to-end test of this, and there cannot be one against the bundled
mock: ``python-opcua``'s *server* has no continuation-point implementation at all
and ignores ``RequestedMaxReferencesPerNode``, so no mock in this repo can emit
one. Stubbing the session is the only way to reach the loop — which is also why
the Node server went without the drain unnoticed.

No OPC UA server and no MCP transport.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opcua import ua
from opcua_mcp_server.server import browse_children


class _StubServer:
    """An OPC UA server answering a scripted list of browse results."""

    def __init__(self, results):
        self._results = list(results)
        self.browse_calls = 0
        self.browse_next_calls = []

    def browse(self, _params):
        self.browse_calls += 1
        return [self._results[0]]

    def browse_next(self, params):
        self.browse_next_calls.append(
            (list(params.ContinuationPoints), params.ReleaseContinuationPoints)
        )
        return [self._results[len(self.browse_next_calls)]]


def _result(status, reference_names, continuation_point):
    """One ``BrowseResult``, with just the fields ``browse_children`` reads."""
    references = [
        SimpleNamespace(NodeId=ua.NodeId(index, 2), BrowseName=ua.QualifiedName(name, 2))
        for index, name in enumerate(reference_names, start=1)
    ]
    return SimpleNamespace(
        StatusCode=ua.StatusCode(status),
        References=references,
        ContinuationPoint=continuation_point,
    )


def _node(results):
    server = _StubServer(results)
    return SimpleNamespace(nodeid=ua.NodeId(1, 2), server=server), server


def test_follows_continuation_points_until_the_server_stops_sending_them():
    node, server = _node(
        [
            _result(ua.StatusCodes.Good, ["Tag1", "Tag2"], b"\xaa"),
            _result(ua.StatusCodes.Good, ["Tag3"], b"\xbb"),
            _result(ua.StatusCodes.Good, ["Tag4"], None),
        ]
    )

    children = browse_children(node)

    assert len(children) == 4, "every page must be collected, not just the first"
    assert server.browse_calls == 1
    assert len(server.browse_next_calls) == 2


def test_never_releases_a_continuation_point_it_still_wants_the_rest_of():
    # ReleaseContinuationPoints=True tells the server to throw the remainder
    # away. Sending it here would truncate the answer while looking like paging.
    node, server = _node(
        [
            _result(ua.StatusCodes.Good, ["Tag1"], b"\xaa"),
            _result(ua.StatusCodes.Good, ["Tag2"], None),
        ]
    )

    browse_children(node)

    points, release = server.browse_next_calls[0]
    assert points == [b"\xaa"]
    assert release is False


def test_an_empty_continuation_point_ends_the_walk():
    # Servers send b"" rather than None for "no more" often enough to matter.
    node, server = _node([_result(ua.StatusCodes.Good, ["Tag1"], b"")])

    children = browse_children(node)

    assert len(children) == 1
    assert server.browse_next_calls == [], "must not ask for a page that is not there"


def test_a_bad_status_on_the_first_result_is_an_error_not_an_empty_list():
    node, _ = _node([_result(ua.StatusCodes.BadNodeIdUnknown, [], None)])

    with pytest.raises(ValueError, match="Browse failed with status: BadNodeIdUnknown"):
        browse_children(node)


def test_a_bad_status_on_a_continued_result_is_an_error_not_a_short_list():
    # The case that matters most: an expired continuation point answers with a
    # bad status and no references. Unchecked, the loop would end and return
    # page one as a complete, successful answer.
    node, _ = _node(
        [
            _result(ua.StatusCodes.Good, ["Tag1"], b"\xaa"),
            _result(ua.StatusCodes.BadContinuationPointInvalid, [], None),
        ]
    )

    with pytest.raises(ValueError, match="Browse failed with status: BadContinuationPointInvalid"):
        browse_children(node)
