"""python-opcua read adapter. Native data and exceptions end at this boundary."""

from __future__ import annotations

import asyncio
from typing import Any

from opcua import ua

from ..application.read import ReadRecord
from ..datetimes import format_iso_utc
from ..errors import AdapterFailure, describe_error, message
from ..node_ids import canonical_node_id
from ..node_metadata import AnalogInfo
from ..records import variant_to_json


def _data_type_name(variant: Any) -> str | None:
    name = getattr(getattr(variant, "VariantType", None), "name", None)
    return None if name in (None, "Null") else str(name)


def node_value_record(node_id: str, data_value: Any, engineering: AnalogInfo | None = None) -> dict:
    """One node's reading as a canonical record (``resultShapes.nodeValues``).

    The value goes through the *shared* codec, so a Boolean is ``true`` on both
    runtimes rather than ``True`` here and ``true`` there, and an Int64 is a
    number or a numeric string rather than node-opcua's ``[high, low]`` pair.
    Reading used to stringify natively and so diverged by construction — the one
    thing ``value-encoding.json`` exists to prevent, just outside its reach.
    """
    status = getattr(data_value, "StatusCode", None)
    good = status is None or status.is_good()
    value = getattr(data_value, "Value", None)
    return {
        "node_id": canonical_node_id(node_id),
        "value": variant_to_json(value) if good else None,
        "data_type": _data_type_name(value) if good else None,
        # An absent status code means Good in OPC UA, so name it rather than null.
        "status": str(status.name) if status is not None else "Good",
        "source_timestamp": format_iso_utc(getattr(data_value, "SourceTimestamp", None)),
        "server_timestamp": format_iso_utc(getattr(data_value, "ServerTimestamp", None)),
        # What the plant says this number means. null for most nodes, because
        # only an AnalogItemType publishes it — but on the ones that do it is the
        # difference between "51.75" and "51.75 °C, normal range 0 to 150".
        "engineering": engineering.to_json() if engineering else None,
    }


class PythonOpcuaReadPort:
    def __init__(self, client, metadata):
        self.client = client
        self.metadata = metadata

    async def values(self, node_ids: list[str]) -> list[ReadRecord]:
        def read():
            # Translate before asyncio transfers the exception across threads:
            # Python 3.10 can replace timeout exceptions during that transfer.
            try:
                nodes = [self.client.get_node(node_id) for node_id in node_ids]
                values = self.client.uaclient.get_attributes(
                    [node.nodeid for node in nodes], ua.AttributeIds.Value
                )
                return [
                    node_value_record(node_id, value)
                    for node_id, value in zip(node_ids, values, strict=True)
                ]
            except Exception as error:
                raise AdapterFailure(
                    "read", message("readFailed", reason=describe_error(error)), error
                ) from error

        return await asyncio.to_thread(read)

    async def engineering(self, node_ids: list[str]) -> dict[str, dict | None]:
        def read_metadata():
            try:
                entries = self.metadata.for_nodes(self.client, node_ids)
                return {
                    node_id: info.to_json() if info else None for node_id, info in entries.items()
                }
            except Exception as error:
                raise AdapterFailure(
                    "read", message("readFailed", reason=describe_error(error)), error
                ) from error

        return await asyncio.to_thread(read_metadata)
