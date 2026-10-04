"""Maintained client construction keeps bounded protocol ownership per instance."""

from asyncua.client.ua_client import UASocketProtocol
from opcua_mcp_server.adapters.asyncua_client import ApplicationOwnedClient
from opcua_mcp_server.adapters.asyncua_transport import BoundedProtocol, contract_limits


def test_native_protocol_factory_is_bounded_without_patching_library_classes():
    original = UASocketProtocol.data_received
    first = ApplicationOwnedClient("opc.tcp://localhost:4840")
    second = ApplicationOwnedClient("opc.tcp://localhost:4841")
    assert first.name == first.description == "OPC UA MCP Client"
    selected = first.uaclient._make_protocol()
    other = second.uaclient._make_protocol()
    assert isinstance(selected, BoundedProtocol)
    assert selected is not other
    assert selected._connection._limits == contract_limits()
    assert selected.pre_request_hook == first._wait_until_ready
    assert selected.on_connection_lost == first.uaclient._on_transport_lost
    assert selected.is_session_closing == first.uaclient._is_session_closing
    assert UASocketProtocol.data_received is original
