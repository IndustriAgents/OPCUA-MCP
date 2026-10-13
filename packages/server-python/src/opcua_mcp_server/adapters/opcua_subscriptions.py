"""python-opcua subscription adapter keeps clients and monitored items behind the port."""

from __future__ import annotations

import asyncio

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message


class PythonOpcuaSubscriptionPort:
    def __init__(self, client, manager, metadata):
        self.client = client
        self.manager = manager
        self.metadata = metadata

    def list(self):
        return self.manager.list()

    async def _run(self, operation, function):
        def invoke():
            try:
                return function()
            except ApplicationRefusal:
                raise
            except Exception as error:
                raise AdapterFailure(operation, describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def ranges(self, node_ids):
        return await self._run(
            "subscription-metadata",
            lambda: {
                node_id: info is not None and info.eu_range is not None
                for node_id, info in self.metadata.for_nodes(self.client(), node_ids).items()
            },
        )

    async def subscribe(self, node_id, options, data_filter):
        return await self._run(
            "subscribe",
            lambda: self.manager.subscribe(
                node_id,
                options.get("publishingInterval"),
                options.get("samplingInterval"),
                options.get("bufferSize"),
                data_filter,
            ),
        )

    async def unsubscribe(self, subscription_id):
        def remove():
            try:
                return self.manager.unsubscribe(subscription_id)
            except KeyError as error:
                raise ApplicationRefusal(
                    message("unknownSubscription", subscription_id=subscription_id)
                ) from error

        return await self._run("unsubscribe", remove)
