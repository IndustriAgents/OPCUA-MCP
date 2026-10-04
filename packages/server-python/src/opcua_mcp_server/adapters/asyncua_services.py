"""Instance-owned maintained services for the existing blocking native adapters.

Application ports remain async. Their worker threads cross this boundary to a
private SDK event loop; only asyncua encodes requests and parses network data.
"""

from __future__ import annotations

import asyncio
import contextlib
from concurrent import futures

from asyncua import sync, ua
from asyncua.crypto import security_policies
from opcua import ua as legacy

from .asyncua_client import ApplicationOwnedClient
from .asyncua_values import native_request


def _native(value):
    if isinstance(value, sync.SyncNode):
        return value.aio_obj
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    return native_request(value)


def _post(loop, operation):
    try:
        return loop.post(operation)
    except ua.UaStatusCodeError as error:
        # Preserve the existing numeric classifier and anticipated error frames.
        raise legacy.UaStatusCodeError(error.code) from error
    except asyncio.TimeoutError as error:
        # asyncio.TimeoutError is a separate class on the supported Python 3.10.
        raise futures.TimeoutError() from error


class ServiceView:
    def __init__(self, loop, client):
        self.loop, self.client = loop, client

    def _call(self, name, *args):
        return _post(self.loop, getattr(self.client, name)(*[_native(arg) for arg in args]))

    def get_attributes(self, nodes, attribute):
        return self._call("read_attributes", nodes, attribute)

    def set_attributes(self, nodes, values, attribute=legacy.AttributeIds.Value):
        return self._call("write_attributes", nodes, values, attribute)

    def browse(self, parameters):
        return self._call("browse", parameters)

    def browse_next(self, parameters):
        return self._call("browse_next", parameters)

    def translate_browsepaths_to_nodeids(self, paths):
        return self._call("translate_browsepaths_to_nodeids", paths)

    def history_read(self, parameters):
        return self._call("history_read", parameters)

    def call(self, methods):
        return self._call("call", methods)


class MaintainedNode(sync.SyncNode):
    def __init__(self, owner, node):
        super().__init__(owner.tloop, node)
        self.owner = owner
        self.server = owner.uaclient

    def _call(self, name, *args, **kwargs):
        result = _post(
            self.tloop,
            getattr(self.aio_obj, name)(
                *[_native(arg) for arg in args],
                **{key: _native(value) for key, value in kwargs.items()},
            ),
        )
        return self.owner.wrap_nodes(result)

    def get_value(self):
        return self._call("read_value")

    def get_data_value(self):
        return self._call("read_data_value")

    def get_browse_name(self):
        return self._call("read_browse_name")

    def get_node_class(self):
        return self._call("read_node_class")

    def get_data_type(self):
        return self._call("read_data_type")

    def get_data_type_as_variant_type(self):
        return legacy.VariantType(self._call("read_data_type_as_variant_type").value)

    def get_child(self, path):
        return self._call("get_child", path)

    def get_children(self, **kwargs):
        return self._call("get_children", **kwargs)

    def get_properties(self):
        return self._call("get_properties")

    def get_parent(self):
        return self._call("get_parent")

    def get_references(self, **kwargs):
        return self._call("get_references", **kwargs)

    def call_method(self, method, *arguments):
        return self._call("call_method", method, *arguments)

    def history_read(self, details):
        return self._call("history_read", details)

    def history_read_events(self, details):
        return self._call("history_read_events", details)


class MaintainedSubscription(sync.Subscription):
    @property
    def subscription_id(self):
        return self.aio_obj.subscription_id

    def _subscribe(self, nodes, attribute, mfilter=None, queuesize=0):
        return _post(
            self.tloop,
            self.aio_obj._subscribe(
                _native(nodes), _native(attribute), mfilter=_native(mfilter), queuesize=queuesize
            ),
        )

    def modify_monitored_item(self, handle, sampling, size):
        return _post(self.tloop, self.aio_obj.modify_monitored_item(handle, sampling, size))

    def subscribe_data_change(self, nodes, queuesize=0):
        return self._subscribe(nodes, legacy.AttributeIds.Value, queuesize=queuesize)

    def delete(self):
        return _post(self.tloop, self.aio_obj.delete())

    def subscribe_events(self, source, evfilter=None, queuesize=0):
        return _post(
            self.tloop,
            self.aio_obj.subscribe_events(
                _native(source), evfilter=_native(evfilter), queuesize=queuesize
            ),
        )


class MaintainedClient(sync.Client):
    def __init__(self, url, timeout=4):
        # Construct before starting the thread so a malformed URL cannot leak it.
        self.aio_obj = ApplicationOwnedClient(url, timeout=timeout)
        self.tloop = sync.ThreadLoop(120)
        self.close_tloop = True
        self._closed = False
        self.uaclient = ServiceView(self.tloop, self.aio_obj.uaclient)
        self.tloop.start()

    @property
    def application_name(self):
        return self.aio_obj.name

    @application_name.setter
    def application_name(self, name):
        self.aio_obj.name = name
        self.aio_obj.description = name

    @property
    def session_timeout(self):
        return self.aio_obj.session_timeout

    @session_timeout.setter
    def session_timeout(self, value):
        self.aio_obj.session_timeout = value

    @property
    def secure_channel_timeout(self):
        return self.aio_obj.secure_channel_timeout

    @secure_channel_timeout.setter
    def secure_channel_timeout(self, value):
        self.aio_obj.secure_channel_timeout = value

    def connect(self):
        try:
            return _post(self.tloop, self.aio_obj.connect())
        except BaseException:
            with contextlib.suppress(Exception):
                self.disconnect()
            raise

    def disconnect(self):
        if self._closed:
            return
        self._closed = True
        try:
            _post(self.tloop, self.aio_obj.disconnect())
        finally:
            self.tloop.stop()

    def set_security(self, policy, certificate, key, server_certificate_path=None, mode=None):
        selected = getattr(security_policies, policy.__name__)
        return _post(
            self.tloop,
            self.aio_obj.set_security(
                selected,
                certificate,
                key,
                server_certificate=server_certificate_path,
                mode=ua.MessageSecurityMode(mode.value),
            ),
        )

    def get_node(self, node_id):
        return MaintainedNode(self, self.aio_obj.get_node(_native(node_id)))

    def get_root_node(self):
        return self.get_node(ua.ObjectIds.RootFolder)

    def get_objects_node(self):
        return self.get_node(ua.ObjectIds.ObjectsFolder)

    def wrap_nodes(self, result):
        from asyncua.common.node import Node

        if isinstance(result, Node):
            return MaintainedNode(self, result)
        if isinstance(result, list):
            return [self.wrap_nodes(item) for item in result]
        return result

    def create_subscription(self, period, handler, publishing=True):
        wrapped = sync._SubHandler(self.tloop, handler)
        created = _post(
            self.tloop, self.aio_obj.create_subscription(_native(period), wrapped, publishing)
        )
        return MaintainedSubscription(self.tloop, created)
