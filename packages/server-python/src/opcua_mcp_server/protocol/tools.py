"""Stable tool names map to their protocol handlers, without native services."""

from types import MappingProxyType

from .monitoring_tools import (
    acknowledge_alarm as acknowledge_alarm,
)
from .monitoring_tools import (
    act_on_alarm as act_on_alarm,
)
from .monitoring_tools import (
    list_active_alarms as list_active_alarms,
)
from .monitoring_tools import (
    list_subscriptions as list_subscriptions,
)
from .monitoring_tools import (
    read_event_history as read_event_history,
)
from .monitoring_tools import (
    read_events as read_events,
)
from .monitoring_tools import (
    subscribe_events as subscribe_events,
)
from .monitoring_tools import (
    subscribe_opcua_nodes as subscribe_opcua_nodes,
)
from .monitoring_tools import (
    unsubscribe_opcua_nodes as unsubscribe_opcua_nodes,
)
from .value_tools import (
    browse_opcua_nodes as browse_opcua_nodes,
)
from .value_tools import (
    call_opcua_method as call_opcua_method,
)
from .value_tools import (
    get_server_status as get_server_status,
)
from .value_tools import (
    read_opcua_history as read_opcua_history,
)
from .value_tools import (
    read_opcua_nodes as read_opcua_nodes,
)
from .value_tools import (
    write_opcua_nodes as write_opcua_nodes,
)

TOOL_HANDLERS = MappingProxyType(
    {
        "read_opcua_nodes": read_opcua_nodes,
        "read_opcua_history": read_opcua_history,
        "get_server_status": get_server_status,
        "browse_opcua_nodes": browse_opcua_nodes,
        "write_opcua_nodes": write_opcua_nodes,
        "call_opcua_method": call_opcua_method,
        "subscribe_opcua_nodes": subscribe_opcua_nodes,
        "list_subscriptions": list_subscriptions,
        "unsubscribe_opcua_nodes": unsubscribe_opcua_nodes,
        "read_event_history": read_event_history,
        "subscribe_events": subscribe_events,
        "read_events": read_events,
        "list_active_alarms": list_active_alarms,
        "acknowledge_alarm": acknowledge_alarm,
        "act_on_alarm": act_on_alarm,
    }
)


def handler_for(name: str):
    """Return a stateless protocol handler from the immutable catalogue."""
    return TOOL_HANDLERS[name]
