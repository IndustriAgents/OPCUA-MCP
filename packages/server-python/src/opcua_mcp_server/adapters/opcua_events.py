"""python-opcua event adapter normalizes library failures before returning records."""

from __future__ import annotations

import asyncio

from .. import events as native_events
from ..errors import AdapterFailure, describe_error


class PythonOpcuaEventPort:
    def __init__(self, client, events):
        self.client = client
        self.events = events

    async def _run(self, operation, function):
        def invoke():
            try:
                return function()
            except Exception as error:
                raise AdapterFailure(operation, describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def subscribe(self, node_id, severity, size):
        return await self._run(
            "event-subscribe", lambda: self.events.subscribe(self.client, node_id, severity, size)
        )

    async def drain(self, node_id, limit):
        result = self.events.drain(node_id, limit)
        if result is None:
            return None
        records, remaining, dropped, size, resubscribed = result
        return {
            "records": records,
            "remaining": remaining,
            "dropped": dropped,
            "size": size,
            "resubscribed": resubscribed,
        }

    async def history(self, node_id, start, end, wanted, severity):
        def read():
            page = native_events.read_event_history(
                self.client, node_id, start, end, wanted, severity
            )
            return {
                "records": page.records,
                "fetched": page.fetched,
                "continued": page.continued,
                "last_time": page.last_time,
            }

        return await self._run("event-history", read)
