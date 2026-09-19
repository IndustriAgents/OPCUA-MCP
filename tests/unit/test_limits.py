"""The bounds on how much one call may ask for, and that both runtimes share them.

``contract/tools.json`` -> ``limits`` is the home for all three, for the same
reason ``traversal`` is the home for the browse caps: they were the numbers most
likely to be written out twice and drift, and the tool descriptions quote them,
so the promise and the enforcement have to change together.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.limits import MAX_HISTORY_VALUES, history_values, history_was_clipped

LIMITS = CONTRACT["limits"]
NODE_SRC = (ROOT / "packages" / "server-node" / "src" / "tools.ts").read_text()
PYTHON_SRC = (
    ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server" / "server.py"
).read_text()
CONTRACT_TEXT = (ROOT / "contract" / "tools.json").read_text()


def test_every_limit_is_a_positive_whole_number():
    for name, value in LIMITS.items():
        if name.startswith("$"):
            continue
        assert isinstance(value, int) and value > 0, f"limits.{name} is {value!r}"


def test_the_limits_are_the_ones_both_runtimes_enforce():
    """Neither runtime may carry its own copy of a bound.

    The browse caps were duplicated as literals in ``tools.ts`` and ``server.py``
    before ``traversal`` existed, "which is how two servers come to disagree
    about how deep 'deep' is". These are the same kind of number.
    """
    python_limits = (
        ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server" / "limits.py"
    ).read_text()
    node_limits = (ROOT / "packages" / "server-node" / "src" / "limits.ts").read_text()
    for name in ("maxNodesPerRead", "maxHistoryValues", "maxSubscriptions"):
        assert f'LIMITS["{name}"]' in python_limits, f"Python does not read limits.{name}"
        assert f"CONTRACT.limits.{name}" in node_limits, f"Node does not read limits.{name}"

    # And the tool bodies reach the numbers only through those two modules, so
    # there is exactly one place either runtime resolves a bound.
    assert "from .limits import" in PYTHON_SRC, "server.py does not use the limits module"
    assert 'from "./limits.js"' in NODE_SRC, "tools.ts does not use the limits module"
    assert 'CONTRACT["limits"]' not in PYTHON_SRC, "server.py reads the contract's limits directly"
    assert "CONTRACT.limits" not in NODE_SRC, "tools.ts reads the contract's limits directly"


@pytest.mark.parametrize(
    ("limit", "tool"),
    [
        ("maxNodesPerRead", "read_opcua_nodes"),
        ("maxHistoryValues", "read_opcua_history"),
        ("maxSubscriptions", "subscribe_opcua_nodes"),
    ],
)
def test_the_tool_description_quotes_its_limit(limit, tool):
    """A cap a model is not told about is one it will keep walking into.

    ``subscriptions`` says the same of its own numbers: "The tool descriptions
    quote these numbers, so a change here changes what is promised and what is
    enforced together."
    """
    spec = next(entry for entry in CONTRACT["tools"] if entry["name"] == tool)
    described = spec["description"] + json.dumps(spec["inputSchema"])
    assert str(LIMITS[limit]) in described, (
        f"{tool} never mentions limits.{limit} ({LIMITS[limit]}), so a model "
        f"has no way to size a request that will be accepted"
    )


def test_a_raw_history_read_of_everything_is_no_longer_a_thing_that_can_be_asked_for():
    """`num_values: 0` must not still be documented as "every reading"."""
    spec = next(t for t in CONTRACT["tools"] if t["name"] == "read_opcua_history")
    description = spec["inputSchema"]["properties"]["num_values"]["description"]
    assert "every reading in the range" not in description or "not every reading" in description
    assert str(LIMITS["maxHistoryValues"]) in description


def test_the_truncation_notice_exists_and_names_what_to_do_instead():
    notice = CONTRACT["notices"]["historyTruncated"]
    assert "{count}" in notice
    assert "aggregate_function" in notice, (
        "a truncated history read should point at the tool that can answer the "
        "whole range, not merely say it was truncated"
    )


# --- how num_values resolves, from the table both runtimes share ---------------


TABLE = json.loads((ROOT / "tests" / "fixtures" / "history-limits.json").read_text())


def _number(value):
    """A table entry, with "max" standing for the contract's own cap."""
    return MAX_HISTORY_VALUES if value == "max" else value


@pytest.mark.parametrize("case", TABLE["cases"], ids=[case["name"] for case in TABLE["cases"]])
def test_num_values_resolves_as_the_shared_table_says(case):
    assert history_values(_number(case["num_values"])) == _number(case["wanted"])


@pytest.mark.parametrize(
    "case", TABLE["clipping"], ids=[case["name"] for case in TABLE["clipping"]]
)
def test_the_truncation_notice_fires_when_the_shared_table_says(case):
    clipped = history_was_clipped(_number(case["returned"]), _number(case["wanted"]))
    assert clipped is case["clipped"]
