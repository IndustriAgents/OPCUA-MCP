"""Unit tests for the subscription manager's pure logic and its teardown path.

No OPC UA server and no MCP transport: the OPC UA client is a stand-in, because
what is worth pinning here is the arithmetic both runtimes must agree on (the
resolved intervals and buffer size appear verbatim in every record the agent
reads back) and that shutdown actually deletes what it created.

The Node server's equivalents live in `packages/server-node/test/unit.test.mjs`
and assert the same numbers.
"""

from __future__ import annotations

import pytest
from opcua_mcp_server.subscriptions import (
    DEFAULT_BUFFER_SIZE,
    DEFAULT_PUBLISHING_INTERVAL,
    SubscriptionManager,
    resolve_options,
    unknown_subscription_message,
)


class FakeSubscription:
    """A stand-in for python-opcua's Subscription.

    Only the three methods the manager uses are real: the handler is captured so
    a test can feed it a data change, and `delete` records that it was called.
    """

    def __init__(self, period, handler, deleted, fail_delete=False):
        self.period = period
        self.handler = handler
        self.deleted = deleted
        self.fail_delete = fail_delete
        self.modified: list[tuple] = []

    def subscribe_data_change(self, node, queuesize=0):
        return 1

    def modify_monitored_item(self, handle, new_samp_time, new_queuesize=0):
        self.modified.append((handle, new_samp_time, new_queuesize))

    def delete(self):
        if self.fail_delete:
            raise RuntimeError("session closed")
        self.deleted.append(self)


class FakeClient:
    def __init__(self, deleted, fail_delete=False):
        self.deleted = deleted
        self.fail_delete = fail_delete
        self.subscriptions: list[FakeSubscription] = []

    def get_node(self, node_id):
        return node_id

    def create_subscription(self, period, handler):
        subscription = FakeSubscription(period, handler, self.deleted, self.fail_delete)
        self.subscriptions.append(subscription)
        return subscription


def manager_with(client) -> SubscriptionManager:
    manager = SubscriptionManager()
    manager.attach(client)
    return manager


# --- resolve_options ----------------------------------------------------------
# The Node server resolves the same defaults in `resolveOptions`, and its unit
# suite pins the same numbers.


def test_defaults_an_omitted_request_the_way_the_contract_documents():
    assert resolve_options(None, None, None) == (
        DEFAULT_PUBLISHING_INTERVAL,
        DEFAULT_PUBLISHING_INTERVAL,
        DEFAULT_BUFFER_SIZE,
    )


def test_sampling_interval_zero_means_sample_at_the_publishing_interval():
    assert resolve_options(250, 0, None)[1] == 250
    assert resolve_options(250, None, None)[1] == 250


def test_keeps_a_sampling_interval_faster_than_the_publishing_one():
    publishing, sampling, _ = resolve_options(1000, 100, None)
    assert (publishing, sampling) == (1000, 100)


def test_clamps_a_publishing_interval_nobodys_opcua_server_would_honour():
    assert resolve_options(0, None, None)[0] == 50
    assert resolve_options(-100, None, None)[0] == 50


def test_clamps_the_buffer_so_one_subscription_cannot_grow_without_bound():
    assert resolve_options(None, None, 0)[2] == 1
    assert resolve_options(None, None, 10_000)[2] == 1000
    assert resolve_options(None, None, 7)[2] == 7


def test_falls_back_on_a_non_number_which_mcp_arguments_can_always_be():
    assert resolve_options("nonsense", None, None)[0] == DEFAULT_PUBLISHING_INTERVAL
    assert resolve_options(float("nan"), None, None)[0] == DEFAULT_PUBLISHING_INTERVAL
    assert resolve_options(None, None, True)[2] == DEFAULT_BUFFER_SIZE


# --- the manager --------------------------------------------------------------


def test_subscribe_returns_the_contract_record():
    manager = manager_with(FakeClient([]))
    record = manager.subscribe("ns=2;i=3", 200, 0, 5)
    assert record == {
        "subscription_id": "sub-1",
        "node_id": "ns=2;i=3",
        "publishing_interval": 200,
        "sampling_interval": 200,
        "buffer_size": 5,
        "change_count": 0,
        "changes": [],
    }


def test_an_independent_sampling_rate_is_asked_for_as_a_modification():
    """python-opcua pins a monitored item's sampling rate to the publishing one.

    So the only way to sample faster than the server publishes is to modify the
    item afterwards — and asking for the *same* rate must not do so needlessly.
    """
    client = FakeClient([])
    manager = manager_with(client)

    manager.subscribe("ns=2;i=3", 1000, 100, 5)
    assert client.subscriptions[-1].modified == [(1, 100, 5)]

    manager.subscribe("ns=2;i=4", 1000, 0, 5)
    assert client.subscriptions[-1].modified == []


def test_the_buffer_keeps_the_newest_changes_and_counts_them_all():
    """`change_count` is every change; `changes` is only what still fits."""
    client = FakeClient([])
    manager = manager_with(client)
    manager.subscribe("ns=2;i=3", 200, 0, 2)

    handler = client.subscriptions[0].handler
    for value in (1.0, 2.0, 3.0):
        handler.datachange_notification(None, value, _notification(value))

    record = manager.list()[0]
    assert record["change_count"] == 3
    assert [change["value"] for change in record["changes"]] == [2.0, 3.0]


def test_close_all_deletes_every_subscription_and_empties_the_list():
    deleted: list = []
    manager = manager_with(FakeClient(deleted))
    manager.subscribe("ns=2;i=3")
    manager.subscribe("ns=2;i=4")
    assert [r["subscription_id"] for r in manager.list()] == ["sub-1", "sub-2"]

    manager.close_all()

    assert len(deleted) == 2
    assert manager.list() == []


def test_unsubscribe_deletes_one_and_leaves_the_rest():
    deleted: list = []
    manager = manager_with(FakeClient(deleted))
    manager.subscribe("ns=2;i=3")
    manager.subscribe("ns=2;i=4")

    record = manager.unsubscribe("sub-1")

    assert record["node_id"] == "ns=2;i=3"
    assert len(deleted) == 1
    assert [r["subscription_id"] for r in manager.list()] == ["sub-2"]


def test_close_all_survives_a_subscription_that_refuses_to_delete():
    """Shutdown often runs after the OPC UA server has dropped the session.

    A failing delete there must not turn a tidy exit into a crash.
    """
    manager = manager_with(FakeClient([], fail_delete=True))
    manager.subscribe("ns=2;i=3")

    manager.close_all()

    assert manager.list() == []


def test_an_unknown_id_is_refused():
    manager = manager_with(FakeClient([]))
    with pytest.raises(KeyError):
        manager.unsubscribe("sub-9")
    # The wording the server turns that into, shared with the Node runtime.
    assert unknown_subscription_message("sub-9") == "No such subscription: sub-9"


def test_subscribing_without_a_connected_client_is_refused_readably():
    with pytest.raises(ValueError, match="No OPC UA session available"):
        SubscriptionManager().subscribe("ns=2;i=3")


def _notification(value: float):
    """A stand-in for python-opcua's DataChangeNotif, as the handler sees it."""
    from opcua import ua

    data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    return type("Notif", (), {"monitored_item": type("Item", (), {"Value": data_value})})
