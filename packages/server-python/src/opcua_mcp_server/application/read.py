"""Read use case. No SDK, session, native value or client-library dependency."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from ..errors import ApplicationRefusal, message
from ..limits import chunked


class ReadRecord(TypedDict):
    node_id: str
    value: Any
    data_type: str | None
    status: str
    source_timestamp: str | None
    server_timestamp: str | None
    engineering: dict | None


class ReadPort(Protocol):
    async def values(self, node_ids: list[str]) -> list[ReadRecord]: ...
    async def engineering(self, node_ids: list[str]) -> dict[str, dict | None]: ...


async def read_nodes(port: ReadPort, node_ids: list[str], chunk: int) -> list[ReadRecord]:
    if not node_ids:
        raise ApplicationRefusal(
            message("emptyArray", tool="read_opcua_nodes", argument="node_ids")
        )
    records: list[ReadRecord] = []
    for part in chunked(node_ids, chunk):
        records.extend(await port.values(part))
    engineering = await port.engineering(node_ids)
    return [
        dict(record, engineering=engineering.get(node_id))
        for node_id, record in zip(node_ids, records, strict=True)
    ]
