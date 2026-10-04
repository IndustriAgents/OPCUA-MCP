"""python-opcua write adapter. Prepared native values belong to this one call."""

from __future__ import annotations

import asyncio
from typing import Any

from opcua import ua

from ..application.write import check_eu_range as check_normalized_range
from ..application.write import check_max_change as check_normalized_change
from ..errors import AdapterFailure, ApplicationRefusal, describe_error
from ..limits import LimitExceeded, chunked
from ..policy import as_number
from ..records import variant_to_json
from ..variant_codec import convert_for_variant


def current_value(value: Any) -> dict | None:
    if value is None:
        return None
    status = getattr(value, "StatusCode", None)
    good = status is None or status.is_good()
    return {
        "good": good,
        "status": str(status.name) if status is not None else "Good",
        "number": as_number(variant_to_json(getattr(value, "Value", None))) if good else None,
    }


def check_eu_range(node_id, value, info):
    check_normalized_range(node_id, value, info.to_json() if info else None)


def check_max_change(node_id, value, bound, data_value):
    check_normalized_change(node_id, value, bound.max_change, current_value(data_value))


class PythonOpcuaWritePort:
    def __init__(self, client, metadata):
        self.client = client
        self.metadata = metadata
        self.readings = {}
        self.write_ids = []
        self.write_values = []

    async def _run(self, operation, function):
        def invoke():
            try:
                return function()
            except ApplicationRefusal:
                raise
            except Exception as error:
                raise AdapterFailure(operation, describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def current(self, nodes, indices, chunk):
        def read():
            ids = [self.client.get_node(nodes[index]["node_id"]).nodeid for index in indices]
            values = []
            for part in chunked(ids, chunk):
                values.extend(self.client.uaclient.get_attributes(part, ua.AttributeIds.Value))
            self.readings = dict(zip(indices, values, strict=True))
            return {
                index: current_value(value)
                for index, value in self.readings.items()
                if value is not None
            }

        return await self._run("write-read", read)

    async def engineering(self, node_ids):
        return await self._run(
            "write-metadata",
            lambda: {
                node_id: info.to_json() if info else None
                for node_id, info in self.metadata.for_nodes(self.client, node_ids).items()
            },
        )

    async def prepare(self, node, index):
        def prepare():
            try:
                declared = node.get("data_type")
                if declared:
                    variant_type = ua.VariantType[declared]
                    is_array = isinstance(node.get("value"), (list, tuple))
                else:
                    value = self.readings.get(index)
                    if value is None or not value.StatusCode.is_good():
                        return {
                            "status": str(value.StatusCode.name) if value else "BadUnexpectedError",
                            "error": (
                                "could not read the node's data type to convert the value; "
                                "give data_type to write without reading it first"
                            ),
                        }
                    variant_type = value.Value.VariantType
                    is_array = value.Value.is_array
                converted = convert_for_variant(node.get("value"), variant_type, is_array)
                self.write_ids.append(self.client.get_node(node["node_id"]).nodeid)
                self.write_values.append(ua.DataValue(ua.Variant(converted, variant_type)))
                return None
            except LimitExceeded as error:
                raise ApplicationRefusal(str(error)) from error
            except Exception as error:
                return {"status": "BadTypeMismatch", "error": str(error)}

        return await self._run("write-prepare", prepare)

    async def send(self):
        return await self._run(
            "write",
            lambda: [
                str(status.name)
                for status in self.client.uaclient.set_attributes(
                    self.write_ids, self.write_values, ua.AttributeIds.Value
                )
            ],
        )
