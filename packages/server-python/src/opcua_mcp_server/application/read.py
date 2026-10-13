"""Read use case. No SDK, session, native value or client-library dependency."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from ..errors import ApplicationRefusal, message
from ..limits import chunked
from ..write_plan import write_access
from .model import Model


class ReadRecord(TypedDict):
    node_id: str
    value: Any
    data_type: str | None
    status: str
    source_timestamp: str | None
    server_timestamp: str | None
    engineering: dict | None
    write_access: dict | None


class ReadPort(Protocol):
    async def values(self, node_ids: list[str]) -> list[ReadRecord]: ...
    async def engineering(self, node_ids: list[str]) -> dict[str, dict | None]: ...


async def read_nodes(
    port: ReadPort, node_ids: list[str], chunk: int, model: Model | None = None
) -> list[ReadRecord]:
    """Each node's reading, and with ``model``, how it may be written (``write_access``).

    ``write_access`` is null unless asked for (``include_write_access``): the
    attributes it needs are read only then, once per node per session, and like
    the engineering record they are never a reason the read fails.
    """
    if not node_ids:
        raise ApplicationRefusal(
            message("emptyArray", tool="read_opcua_nodes", argument="node_ids")
        )
    records: list[ReadRecord] = []
    for part in chunked(node_ids, chunk):
        records.extend(await port.values(part))
    engineering = await port.engineering(node_ids)
    records = [
        dict(record, engineering=engineering.get(node_id))
        for node_id, record in zip(node_ids, records, strict=True)
    ]
    if model is not None:
        facts = await model.port.facts(node_ids)
        for record, node_id in zip(records, node_ids, strict=True):
            record["write_access"] = write_access(
                {
                    "node_id": node_id,
                    "facts": facts.get(node_id),
                    "engineering": engineering.get(node_id),
                    "policy": model.write_policy(node_id),
                }
            )
    return records
