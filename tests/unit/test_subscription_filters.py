"""What a subscription reports, beyond how often it looks (issue #118).

`subscribe_opcua_nodes` took `publishing_interval`, `sampling_interval` and
`buffer_size` — and no filter. Point it at a noisy analogue tag and the default
20-record ring fills with sensor jitter in about a second: the agent reads it
back, sees nothing but noise, and has spent one of the server's 200 subscriptions
to get it.

OPC UA's answer is `DataChangeFilter` (Part 4 §7.22), and it is better than
filtering after the fact because the values never leave the server — no
bandwidth, no buffer, no round trip.

`packages/server-node/test/subscription-filters.test.mjs` is the Node half and
drives the same shared table. What is *not* here is the percent deadband's
EURange requirement, which needs a real node; that is in
`tests/e2e/test_engineering_units_e2e.py`.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua import ua
from opcua.common.subscription import Subscription
from opcua_mcp_server.subscriptions import (
    DATA_CHANGE_TRIGGERS,
    DEADBAND_TYPES,
    DEFAULT_DATA_CHANGE_TRIGGER,
    Filter,
    resolve_filter,
)

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "subscription-filters.json").read_text(encoding="utf-8")
)["cases"]


def call(arguments: dict) -> Filter:
    return resolve_filter(
        arguments.get("deadband_type"),
        arguments.get("deadband_value"),
        arguments.get("data_change_trigger"),
    )


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_filter_answers_the_shared_table(case):
    """The Node half resolves the same request the same way."""
    if "error" in case:
        with pytest.raises(ValueError) as raised:
            call(case["arguments"])
        assert str(raised.value) == case["error"]
        return

    resolved = call(case["arguments"])
    assert resolved.deadband_type == case["filter"]["deadband_type"]
    assert resolved.deadband_value == case["filter"]["deadband_value"]
    assert resolved.trigger == case["filter"]["trigger"]


# --- what the names mean to the library ------------------------------------------


@pytest.mark.parametrize("name", sorted(DEADBAND_TYPES))
def test_every_deadband_the_contract_names_exists_in_the_library(name):
    """The contract names them; python-opcua supplies the numbers.

    Same idea as the dead-session status codes (#112): a name the library stops
    publishing fails here rather than quietly resolving to nothing. The numbering
    is fixed by Part 4 §7.22, which is what lets the Node half map the same names
    onto *its* library and provably agree.
    """
    assert DEADBAND_TYPES[name] == getattr(
        ua.DeadbandType, "None_" if name == "none" else name.capitalize()
    )


@pytest.mark.parametrize("name", sorted(DATA_CHANGE_TRIGGERS))
def test_every_trigger_the_contract_names_exists_in_the_library(name):
    assert DATA_CHANGE_TRIGGERS[name] == getattr(ua.DataChangeTrigger, name[0].upper() + name[1:])


def test_the_default_trigger_is_not_opc_uas_default():
    """OPC UA defaults a DataChangeFilter to `Status`, and that is the wrong default here.

    An agent that asked to watch a *value* and was told only about status
    transitions would have been given something nobody asks for.
    """
    assert DEFAULT_DATA_CHANGE_TRIGGER == "statusValue"
    assert ua.DataChangeTrigger.Status == 0, "OPC UA's own default, for contrast"


def test_python_opcua_still_lets_a_filter_be_attached_at_creation():
    """The one private reach in `subscriptions.py`, pinned so it fails loudly.

    `subscribe_data_change` is `_subscribe` with `mfilter=None` hardcoded, and
    python-opcua offers no public way to attach a general `DataChangeFilter` when
    the item is created — `deadband_monitor` fixes the trigger and
    `modify_monitored_item` can only express an absolute deadband, and attaching
    one afterwards would leave a window in which the unfiltered item is already
    delivering.

    The library is unmaintained, so this is unlikely to move; if it ever does,
    this test is what says so rather than subscriptions silently losing their
    filters.
    """
    assert hasattr(Subscription, "_subscribe")
    parameters = Subscription._subscribe.__code__.co_varnames
    assert "mfilter" in parameters, f"_subscribe no longer takes a filter: {parameters}"


# --- the default is still the default --------------------------------------------


def test_a_default_filter_asks_the_server_for_nothing():
    """No `DataChangeFilter` is sent at all when none was wanted.

    A server is entitled to reject a filter it does not implement, and there is
    no reason to risk that for a subscription that asked for nothing special.
    """
    assert Filter().is_default
    assert Filter(trigger="statusValue").is_default
    assert not Filter(deadband_type="absolute", deadband_value=1).is_default
    assert not Filter(trigger="status").is_default
