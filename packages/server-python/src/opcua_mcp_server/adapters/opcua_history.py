"""python-opcua history adapter owns native requests, values and continuation points."""

from __future__ import annotations

import asyncio

from opcua import ua

from ..errors import AdapterFailure, ApplicationRefusal, describe_error
from ..history import (
    aggregate_pages,
    continues,
    raw_details,
    read_continuation,
    release_continuation_point,
)
from ..limits import LimitExceeded
from ..records import history_data, history_records


class PythonOpcuaHistoryPort:
    def __init__(self, client, aggregate_functions):
        self.client = client
        self.aggregate_functions = aggregate_functions

    async def _run(self, operation, function):
        def invoke():
            try:
                return function()
            except LimitExceeded as error:
                if operation == "history-aggregate":
                    raise ApplicationRefusal(str(error)) from error
                raise AdapterFailure(operation, describe_error(error), error) from error
            except Exception as error:
                raise AdapterFailure(operation, describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def raw(self, node_id, start, end, wanted):
        def read():
            details = raw_details(start, end, wanted)
            result = self.client.get_node(node_id).history_read(details)
            values = history_data(result, "Read history", "DataValues")
            continued = continues(result.ContinuationPoint)
            release_continuation_point(self.client, node_id, result.ContinuationPoint, details)
            return {"records": history_records(values), "continued": continued}

        return await self._run("history-raw", read)

    async def aggregate(self, node_id, start, end, name, interval):
        def read():
            details = ua.ReadProcessedDetails()
            details.StartTime = start
            details.EndTime = end
            details.ProcessingInterval = interval
            details.AggregateType = [self.aggregate_functions[name]]
            first = self.client.get_node(node_id).history_read(details)
            values = aggregate_pages(
                first,
                lambda point: read_continuation(self.client, node_id, point, details),
                lambda point: release_continuation_point(self.client, node_id, point, details),
            )
            return history_records(values)

        return await self._run("history-aggregate", read)
