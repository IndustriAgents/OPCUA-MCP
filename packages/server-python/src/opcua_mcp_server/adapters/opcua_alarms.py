"""python-opcua alarm adapter selects a client only when a native service is required."""

from __future__ import annotations

import asyncio

from .. import events as native_events
from ..errors import AdapterFailure, describe_error


class PythonOpcuaAlarmPort:
    def __init__(self, client, events):
        self.client = client
        self.events = events

    def remember(self, records):
        self.events().remember(records)

    def condition_for(self, event_id):
        return self.events().condition_for(event_id)

    async def _run(self, operation, function):
        def invoke():
            try:
                return function()
            except Exception as error:
                raise AdapterFailure(operation, describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def list(self, node_id, timeout):
        return await self._run(
            "alarms", lambda: native_events.list_active_alarms(self.client(), node_id, timeout)
        )

    async def action(self, condition_id, event_id, action, comment, duration):
        return await self._run(
            "alarm-action",
            lambda: {
                "status": native_events.alarm_action(
                    self.client(), condition_id, event_id, action, comment, duration
                ),
                "good": True,
            },
        )
