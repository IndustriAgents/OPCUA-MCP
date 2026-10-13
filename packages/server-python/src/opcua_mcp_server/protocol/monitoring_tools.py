"""MCP tool signatures and result conversion around injected application ports."""

from __future__ import annotations

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from .. import events
from ..adapters.opcua_alarms import PythonOpcuaAlarmPort
from ..adapters.opcua_events import PythonOpcuaEventPort
from ..adapters.opcua_subscriptions import PythonOpcuaSubscriptionPort
from ..application.alarms import act_on_alarm as act_on_alarm_use_case
from ..application.alarms import list_alarms
from ..application.events import read_event_history as read_event_history_use_case
from ..application.events import read_events as read_events_use_case
from ..application.events import subscribe_events as subscribe_events_use_case
from ..application.subscriptions import list_subscriptions as list_subscriptions_use_case
from ..application.subscriptions import subscribe_nodes, unsubscribe_nodes
from ..contract import CONTRACT
from ..errors import AdapterFailure, ApplicationRefusal
from ..errors import message as error_message
from ..limits import (
    LimitExceeded,
)
from ..subscriptions import (
    resolve_filter,
)
from .results import _history_result, _object_result, _records_result, _state

_TRAVERSAL = CONTRACT["traversal"]


def _subscription_port(ctx: Context):
    state = _state(ctx)
    return PythonOpcuaSubscriptionPort(
        lambda: ctx.request_context.lifespan_context["opcua_client"],
        state.subscriptions,
        state.node_metadata,
    )


async def subscribe_opcua_nodes(
    node_ids: list[str],
    ctx: Context,
    publishing_interval: float = 1000,
    sampling_interval: float = 0,
    buffer_size: int = 20,
    deadband_type: str | None = None,
    deadband_value: float | None = None,
    data_change_trigger: str | None = None,
) -> CallToolResult:
    """Watch nodes for changes, validating percent ranges before creating subscriptions."""
    if not node_ids:
        raise ToolError(
            error_message("emptyArray", tool="subscribe_opcua_nodes", argument="node_ids")
        )
    try:
        data_filter = resolve_filter(deadband_type, deadband_value, data_change_trigger)
        result = await subscribe_nodes(
            _subscription_port(ctx),
            node_ids,
            {
                "publishingInterval": publishing_interval,
                "samplingInterval": sampling_interval,
                "bufferSize": buffer_size,
            },
            data_filter,
        )
        return _records_result(result["records"], result["completeness"])
    except (ValueError, AdapterFailure) as error:
        raise ToolError(str(error)) from error


def list_subscriptions(ctx: Context) -> CallToolResult:
    """Read existing buffers even when the OPC UA connection is unavailable."""
    result = list_subscriptions_use_case(_subscription_port(ctx))
    return _records_result(result["records"], result["completeness"])


async def unsubscribe_opcua_nodes(subscription_ids: list[str], ctx: Context) -> CallToolResult:
    """Validate every subscription ID before cancelling any subscription."""
    if not subscription_ids:
        raise ToolError(
            error_message("emptyArray", tool="unsubscribe_opcua_nodes", argument="subscription_ids")
        )
    try:
        result = await unsubscribe_nodes(_subscription_port(ctx), subscription_ids)
        return _records_result(result["records"], result["completeness"])
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


async def read_event_history(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    start_time: str | None = None,
    end_time: str | None = None,
    num_values: int = 0,
    severity_min: int = events.DEFAULTS["severityMin"],
) -> CallToolResult:
    """Read the events the server stored, for a range that has already passed."""
    port = PythonOpcuaEventPort(
        ctx.request_context.lifespan_context["opcua_client"], _state(ctx).events
    )
    try:
        result = await read_event_history_use_case(
            port,
            {
                "node_id": node_id,
                "start": start_time,
                "end": end_time,
                "num_values": num_values,
                "severity_min": severity_min,
            },
        )
        return _history_result(result["records"], result["completeness"], "eventHistoryTruncated")
    except (ApplicationRefusal, AdapterFailure, LimitExceeded) as error:
        raise ToolError(str(error)) from error


async def subscribe_events(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    severity_min: int = events.DEFAULTS["severityMin"],
    buffer_size: int = events.DEFAULTS["bufferSize"],
) -> CallToolResult:
    """Start buffering OPC UA events from a notifier node."""
    port = PythonOpcuaEventPort(
        ctx.request_context.lifespan_context["opcua_client"], _state(ctx).events
    )
    try:
        return _object_result(
            await subscribe_events_use_case(port, node_id, severity_min, buffer_size)
        )
    except AdapterFailure as error:
        raise ToolError(str(error)) from error


async def read_events(
    ctx: Context, node_id: str = events.DEFAULT_NOTIFIER, limit: int = events.DEFAULTS["readLimit"]
) -> CallToolResult:
    """Drain buffered events and report truncation, overflow and reconnect gaps."""
    port = PythonOpcuaEventPort(
        ctx.request_context.lifespan_context["opcua_client"], _state(ctx).events
    )
    try:
        result = await read_events_use_case(port, node_id, limit or events.DEFAULTS["readLimit"])
    except ApplicationRefusal as error:
        raise ToolError(str(error)) from error
    response = _records_result(result["records"], result["completeness"])
    response.content.extend(TextContent(type="text", text=text) for text in result["notices"])
    return response


def _alarm_port(ctx: Context):
    return PythonOpcuaAlarmPort(
        lambda: ctx.request_context.lifespan_context["opcua_client"], lambda: _state(ctx).events
    )


async def list_active_alarms(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    timeout_seconds: float = events.DEFAULTS["refreshTimeoutSeconds"],
) -> list[dict]:
    """List retained alarm/condition instances and remember their EventIds."""
    try:
        return await list_alarms(_alarm_port(ctx), node_id, timeout_seconds)
    except AdapterFailure as error:
        raise ToolError(str(error)) from error


async def acknowledge_alarm(
    event_id: str, ctx: Context, comment: str = "", condition_id: str | None = None
) -> CallToolResult:
    """Acknowledge an alarm, preserving the dedicated acknowledgement result shape."""
    try:
        return _object_result(
            await act_on_alarm_use_case(
                _alarm_port(ctx), event_id, "acknowledge", comment, None, condition_id, True
            )
        )
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


async def act_on_alarm(
    event_id: str,
    action: str,
    ctx: Context,
    comment: str = "",
    shelve_duration_ms: float | None = None,
    condition_id: str | None = None,
) -> CallToolResult:
    """Apply an alarm action, retaining the action tool's result shape and frame."""
    try:
        return _object_result(
            await act_on_alarm_use_case(
                _alarm_port(ctx), event_id, action, comment, shelve_duration_ms, condition_id, False
            )
        )
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error
