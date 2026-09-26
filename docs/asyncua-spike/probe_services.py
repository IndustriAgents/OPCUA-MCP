# ruff: noqa: E501
"""The services the Python runtime uses, through asyncua 2.0.1, against the mocks.

Against packages/mock-server (python-opcua): browse and BrowseNext, batched
Read/Write and Variant typing, InputArguments and a Duration-typed method call,
raw history and event history with continuation points, data-change
subscriptions with deadband filters, event subscriptions with the contract's
select clauses, and what a request raises once the server is gone.
Against packages/mock-server-aggregate (node-opcua): ReadProcessed.

    uv run --no-sync --with asyncua==2.0.1 python docs/asyncua-spike/probe_services.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from _common import aggregate_mock, load_json, python_mock, report
from asyncua import Client, ua

logging.disable(logging.CRITICAL)
CONTRACT = load_json("contract/tools.json")
EVENTS = CONTRACT["events"]


async def path(client: Client, *names: str) -> ua.NodeId:
    node = await client.nodes.objects.get_child([f"2:{n}" for n in names])
    return node.nodeid


async def browse(client: Client) -> None:
    sensors = await path(client, "IndustrialControlSystem", "Sensors")
    description = ua.BrowseDescription()
    description.NodeId = sensors
    description.BrowseDirection = ua.BrowseDirection.Forward
    description.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    description.IncludeSubtypes = True
    description.NodeClassMask = 0
    description.ResultMask = ua.BrowseResultMask.All
    params = ua.BrowseParameters()
    params.NodesToBrowse = [description]
    params.RequestedMaxReferencesPerNode = 3
    first = (await client.uaclient.browse(params))[0]
    report(
        "note",
        "browse.mock-server",
        f"RequestedMaxReferencesPerNode=3 on Sensors: the python-opcua mock returned "
        f"{len(first.References)} references and continuation point {first.ContinuationPoint!r} "
        f"(its server ignores the cap and answers BrowseNext with BadServiceUnsupported); "
        f"continuation points are exercised against the open62541 lab in probe_lab.py. "
        f"ReferenceDescription.NodeId.to_string()={first.References[0].NodeId.to_string()!r}",
    )


async def read_write(client: Client) -> None:
    names = ["Temperature", "Pressure", "FlowRate", "TankLevel", "Vibration"]
    ids = [await path(client, "IndustrialControlSystem", "Sensors", n) for n in names]
    ids.append(ua.NodeId(999999, 2))
    params = ua.ReadParameters()
    for node_id in ids:
        for attribute in (ua.AttributeIds.Value, ua.AttributeIds.DataType):
            rv = ua.ReadValueId()
            rv.NodeId, rv.AttributeId = node_id, attribute
            params.NodesToRead.append(rv)
    values = await client.uaclient.read(params)
    statuses = sorted({v.StatusCode.name for v in values})
    stamp = values[0].SourceTimestamp
    report(
        "supported",
        "read.batched",
        f"one Read of {len(params.NodesToRead)} ReadValueIds -> {len(values)} DataValues, statuses "
        f"{statuses} (a missing node is a per-item status, not an exception); SourceTimestamp "
        f"tzinfo={stamp.tzinfo}; Value.VariantType={values[0].Value.VariantType.name}",
    )
    scratch = await path(client, "IndustrialControlSystem", "Scratch")
    double = await path(client, "IndustrialControlSystem", "Scratch", "ScratchDouble")
    boolean = await path(client, "IndustrialControlSystem", "Scratch", "ScratchBoolean")
    del scratch
    writes = ua.WriteParameters()
    for node_id, variant in (
        (double, ua.Variant(51.75, ua.VariantType.Double)),
        (boolean, ua.Variant(True, ua.VariantType.Boolean)),
        (double, ua.Variant(7, ua.VariantType.Int32)),
    ):
        wv = ua.WriteValue()
        wv.NodeId, wv.AttributeId = node_id, ua.AttributeIds.Value
        wv.Value = ua.DataValue(variant)
        writes.NodesToWrite.append(wv)
    results = await client.uaclient.write(writes)
    written = await client.get_node(double).read_data_value()
    report(
        "supported",
        "write.batched",
        f"one Write of 3 values -> {[r.name for r in results]}; ScratchDouble now "
        f"{written.Value.Value!r} as {written.Value.VariantType.name} (the python-opcua mock "
        f"does not type-check writes, so the Int32 write is accepted there as it is today)",
    )
    try:
        ua.Variant(1.5, ua.VariantType.Int32)
        built = "constructed"
        ua.ua_binary.variant_to_binary(ua.Variant(1.5, ua.VariantType.Int32))
        encoded = "encoded"
    except Exception as error:
        encoded = f"{type(error).__name__}: {error}"
    report(
        "note",
        "write.variant-typing",
        f"Variant(1.5, Int32) {built}, encode -> {encoded}; the project's variant_codec refuses "
        f"such values before a Variant is built, so the library's leniency is never reached",
    )
    override = await client.get_node(ua.NodeId("OverriddenSetpoint", 2)).read_data_value(
        raise_on_bad_status=False
    )
    report(
        "supported",
        "read.good-subcode",
        f"GoodLocalOverride read -> status {override.StatusCode.name}, value {override.Value.Value!r}",
    )


async def methods(client: Client) -> None:
    folder = await path(client, "IndustrialControlSystem", "Methods")
    echo = client.get_node(ua.NodeId("EchoDuration", 2))
    prop = await echo.get_child("0:InputArguments")
    arguments = await prop.read_value()
    arg = arguments[0]
    report(
        "supported",
        "methods.input-arguments",
        f"InputArguments decode to {type(arg).__name__} (Name={arg.Name!r}, "
        f"DataType={arg.DataType.to_string()}, ValueRank={arg.ValueRank}); python-opcua "
        f"returns the same ua.Argument shape",
    )
    walk, node_id = [], arg.DataType
    while node_id.NamespaceIndex != 0 or node_id.Identifier > 25:
        parents = await client.get_node(node_id).get_references(
            ua.ObjectIds.HasSubtype, ua.BrowseDirection.Inverse
        )
        node_id = parents[0].NodeId
        walk.append(node_id.to_string())
    answer = await client.get_node(folder).call_method(
        echo, ua.Variant(1500.0, ua.VariantType.Double)
    )
    report(
        "supported",
        "methods.duration",
        f"Duration resolved by inverse HasSubtype: i=290 -> {walk}; call with Variant(Double) "
        f"-> server received {answer!r}",
    )
    start = await path(client, "IndustrialControlSystem", "Methods", "StartProduction")
    request = ua.CallMethodRequest()
    request.ObjectId, request.MethodId = folder, start
    request.InputArguments = [ua.Variant(12.5, ua.VariantType.Double)]
    result = (await client.uaclient.call([request]))[0]
    report(
        "supported",
        "methods.call-batched",
        f"uaclient.call([CallMethodRequest]) -> StatusCode {result.StatusCode.name}, outputs "
        f"{[v.Value for v in result.OutputArguments]}, InputArgumentResults "
        f"{[s.name for s in result.InputArgumentResults]}",
    )
    try:
        await client.get_node(folder).call_method(start, ua.Variant("x", ua.VariantType.String))
        wrong = "accepted"
    except ua.UaStatusCodeError as error:
        wrong = f"{type(error).__name__} code={error.code:#x}"
    report(
        "note",
        "methods.call-error",
        f"Node.call_method with a mistyped argument raises {wrong} (python-opcua raises its "
        f"own UaStatusCodeError subclass of the same name)",
    )


async def history(client: Client) -> None:
    temperature = await path(client, "IndustrialControlSystem", "Sensors", "Temperature")
    details = ua.ReadRawModifiedDetails()
    details.IsReadModified = False
    details.StartTime = datetime.now(timezone.utc) - timedelta(minutes=5)
    details.EndTime = datetime.now(timezone.utc)
    details.NumValuesPerNode = 2
    details.ReturnBounds = False
    node = client.get_node(temperature)
    first = await node.history_read(details)
    pages, count, cp = 1, len(first.HistoryData.DataValues), first.ContinuationPoint
    while cp and pages < 20:
        more = await node.history_read(details, cp)
        count += len(more.HistoryData.DataValues)
        cp, pages = more.ContinuationPoint, pages + 1
    report(
        "supported" if pages > 1 else "differs",
        "history.raw",
        f"ReadRawModifiedDetails NumValuesPerNode=2: {count} values over {pages} pages; "
        f"timestamps tzinfo={first.HistoryData.DataValues[0].SourceTimestamp.tzinfo}",
    )
    first = await node.history_read(details)
    params = ua.HistoryReadParameters()
    params.HistoryReadDetails = details
    params.TimestampsToReturn = ua.TimestampsToReturn.Both
    params.ReleaseContinuationPoints = True
    value_id = ua.HistoryReadValueId()
    value_id.NodeId, value_id.ContinuationPoint = temperature, first.ContinuationPoint
    params.NodesToRead = [value_id]
    released = await client.uaclient.history_read(params)
    report(
        "supported",
        "history.release",
        f"HistoryRead(ReleaseContinuationPoints=True) -> {released[0].StatusCode.name} "
        f"(history.py's release_continuation_point, unchanged)",
    )

    server = client.get_node(ua.NodeId(ua.ObjectIds.Server))
    event_details = ua.ReadEventDetails()
    event_details.StartTime = datetime.now(timezone.utc) - timedelta(minutes=5)
    event_details.EndTime = datetime.now(timezone.utc)
    event_details.NumValuesPerNode = 0
    event_details.Filter = event_filter()
    result = await server.history_read_events(event_details)
    from opcua_mcp_server.events import event_record

    records = [event_record(e.EventFields) for e in result.HistoryData.Events]
    report(
        "supported" if records else "note",
        "history.events",
        f"history_read_events with the contract's {len(event_details.Filter.SelectClauses)} "
        f"select clauses: {len(records)} event(s), continuation point "
        f"{'present' if result.ContinuationPoint else 'absent'}; first record "
        f"{records[0] if records else None}",
    )

    # The python-opcua *server* never answers an event HistoryRead that would need a
    # continuation point (python-opcua's own client times out the same way), which
    # makes it a convenient, real request timeout to observe the exception on.
    event_details.NumValuesPerNode = 1
    started = time.monotonic()
    try:
        await server.history_read_events(event_details)
        outcome = "answered"
    except Exception as error:
        outcome = (
            f"{type(error).__module__}.{type(error).__name__}({error!s}) from {error.__cause__!r}"
        )
    report(
        "differs",
        "errors.request-timeout",
        f"a request the server never answers (client timeout 10s) raises {outcome} after "
        f"{time.monotonic() - started:.1f}s. python-opcua raises TimeoutError itself; asyncua "
        f"wraps it in a bare Exception, so only is_connection_error()'s __cause__ walk finds it",
    )


def event_filter() -> ua.EventFilter:
    """events.event_filter(), rebuilt from the contract on asyncua's types."""
    clauses = []
    for field in EVENTS["fields"]:
        operand = ua.SimpleAttributeOperand()
        if field["path"] == "ConditionId":
            operand.TypeDefinitionId = ua.NodeId.from_string(EVENTS["conditionTypeNodeId"])
            operand.BrowsePath = []
            operand.AttributeId = ua.AttributeIds.NodeId
        else:
            operand.TypeDefinitionId = ua.NodeId.from_string(EVENTS["baseEventTypeNodeId"])
            operand.BrowsePath = [ua.QualifiedName(n, 0) for n in field["path"].split(".")]
            operand.AttributeId = ua.AttributeIds.Value
        clauses.append(operand)
    evfilter = ua.EventFilter()
    evfilter.SelectClauses = clauses
    return evfilter


def subscription_parameters() -> ua.CreateSubscriptionParameters:
    request = EVENTS["subscriptionRequest"]
    params = ua.CreateSubscriptionParameters()
    params.RequestedPublishingInterval = request["publishingIntervalMs"]
    params.RequestedLifetimeCount = request["lifetimeCount"]
    params.RequestedMaxKeepAliveCount = request["maxKeepAliveCount"]
    params.MaxNotificationsPerPublish = request["maxNotificationsPerPublish"]
    params.PublishingEnabled = True
    params.Priority = request["priority"]
    return params


class Collector:
    def __init__(self):
        self.changes, self.events = [], []

    def datachange_notification(self, node, value, data):  # sync on purpose
        self.changes.append(value)

    def event_notification(self, event):
        from opcua_mcp_server.events import event_record

        self.events.append(event_record(event.event_fields))


async def subscriptions(client: Client) -> None:
    temperature = await path(client, "IndustrialControlSystem", "Sensors", "Temperature")
    results = {}
    for label, kind, value in (("none", ua.DeadbandType.None_, 0.0), ("absolute 50", ua.DeadbandType.Absolute, 50.0)):  # fmt: skip
        collector = Collector()
        sub = await client.create_subscription(subscription_parameters(), collector)
        mfilter = ua.DataChangeFilter()
        mfilter.Trigger = ua.DataChangeTrigger.StatusValue
        mfilter.DeadbandType = kind
        mfilter.DeadbandValue = value
        request = ua.MonitoredItemCreateRequest()
        request.ItemToMonitor = ua.ReadValueId(
            NodeId=temperature, AttributeId=ua.AttributeIds.Value
        )
        request.MonitoringMode = ua.MonitoringMode.Reporting
        request.RequestedParameters = ua.MonitoringParameters(
            ClientHandle=1, SamplingInterval=100, Filter=mfilter, QueueSize=10, DiscardOldest=True
        )
        try:
            handles = await sub.create_monitored_items([request])
            status = "created"
        except Exception as error:
            handles, status = [], f"{type(error).__name__}: {error}"
        await asyncio.sleep(5)
        results[label] = (status, len(collector.changes), handles)
        await sub.delete()
    report(
        "supported",
        "subscriptions.deadband",
        f"DataChangeFilter via create_monitored_items on Temperature over 5s: "
        f"{ {k: (v[0], v[1]) for k, v in results.items()} } (the mock's value moves ~2 per "
        f"second, so absolute 50 suppresses changes after the initial value)",
    )

    collector = Collector()
    sub = await client.create_subscription(subscription_parameters(), collector)
    handle = await sub.subscribe_events(
        ua.NodeId(ua.ObjectIds.Server), evfilter=event_filter(), queuesize=EVENTS["subscriptionRequest"]["queueSize"]
    )  # fmt: skip
    folder = await path(client, "IndustrialControlSystem", "Methods")
    estop = await path(client, "IndustrialControlSystem", "Methods", "EmergencyStop")
    reset = await path(client, "IndustrialControlSystem", "Methods", "ResetSystem")
    await client.get_node(folder).call_method(estop)
    await asyncio.sleep(2.5)
    await client.get_node(folder).call_method(reset)
    await asyncio.sleep(2.5)
    await sub.delete()
    report(
        "supported" if collector.events else "bug",
        "events.subscription",
        f"subscribe_events(Server, evfilter=<contract select clauses>, queuesize=1000) -> handle "
        f"{handle}; {len(collector.events)} event(s) delivered to a sync handler; first "
        f"{collector.events[0] if collector.events else None}",
    )


async def namespace_array(client: Client) -> list[str]:
    return await client.get_namespace_array()


async def dead_server() -> None:
    with python_mock() as url:
        client = Client(url, timeout=4, watchdog_intervall=1.0)
        await client.connect()
        before = await namespace_array(client)
        watchdog = []
        client.connection_lost_callback = lambda exc: _note(watchdog, exc)
    # the mock is gone here
    started = time.monotonic()
    try:
        await client.nodes.server_state.read_value()
        outcome = "succeeded"
    except BaseException as error:
        outcome = f"{type(error).__module__}.{type(error).__name__}: {error}"
    elapsed = time.monotonic() - started
    await asyncio.sleep(3)
    try:
        await client.nodes.server_state.read_value()
        later = "succeeded"
    except BaseException as error:
        later = f"{type(error).__name__}: {error}"
    report(
        "differs",
        "session.dead-server",
        f"a read right after the server exits raises {outcome} in {elapsed:.2f}s; 3s later "
        f"{later}; connection_lost_callback fired with {watchdog or 'nothing'}; "
        f"uaclient state {client.uaclient.state.name}. With auto_reconnect=False the client "
        f"stays DISCONNECTED and every call raises ConnectionError, which is an OSError, so "
        f"is_connection_error() classifies it as a dead session",
    )
    await client.disconnect()
    del before


async def _note(sink: list, exc: Exception) -> None:
    sink.append(f"{type(exc).__name__}")


async def aggregates() -> None:
    with aggregate_mock(warmup=8) as url:
        client = Client(url, timeout=10)
        async with client:
            node = client.get_node(ua.NodeId(1001, 1))
            details = ua.ReadProcessedDetails()
            details.EndTime = datetime.now(timezone.utc)
            details.StartTime = details.EndTime - timedelta(seconds=6)
            details.ProcessingInterval = 2000
            details.AggregateType = [ua.NodeId(ua.ObjectIds.AggregateFunction_Average)]
            config = ua.AggregateConfiguration()
            config.UseServerCapabilitiesDefaults = True
            details.AggregateConfiguration = config
            params = ua.HistoryReadParameters()
            params.HistoryReadDetails = details
            params.TimestampsToReturn = ua.TimestampsToReturn.Source
            value_id = ua.HistoryReadValueId()
            value_id.NodeId = node.nodeid
            params.NodesToRead = [value_id]
            result = (await client.uaclient.history_read(params))[0]
            values = [(dv.Value.Value, dv.StatusCode.name) for dv in result.HistoryData.DataValues]
            report(
                "supported" if values else "bug",
                "history.aggregate",
                f"ReadProcessedDetails(Average, 2s buckets over 6s) on node-opcua -> "
                f"{result.StatusCode.name}, {len(values)} buckets {values[:3]}",
            )


async def main() -> None:
    with python_mock(warmup=6) as url:
        client = Client(url, timeout=10)
        async with client:
            for probe in (browse, read_write, methods, subscriptions, history):
                try:
                    await probe(client)
                except Exception as error:
                    cause = error.__cause__
                    report(
                        "bug",
                        probe.__name__,
                        f"probe raised {type(error).__name__}: {error} (cause: {cause!r})",
                    )
    await dead_server()
    await aggregates()


if __name__ == "__main__":
    asyncio.run(main())
