"""Write orchestration. A per-call port prepares values and sends at most one service."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id
from ..policy import as_number, format_number
from ..result_text import json_text


class WritePort(Protocol):
    async def current(
        self, nodes: list[dict], indices: list[int], chunk: int
    ) -> dict[int, dict]: ...
    async def engineering(self, node_ids: list[str]) -> dict[str, dict | None]: ...
    async def prepare(self, node: dict, index: int) -> dict | None: ...
    async def send(self) -> list[str]: ...


def check_eu_range(node_id: str, value: Any, info: dict | None) -> None:
    range_ = info.get("eu_range") if info else None
    if not range_:
        return
    number = as_number(value)
    if number is None or range_["low"] <= number <= range_["high"]:
        return
    raise ApplicationRefusal(
        message(
            "valueOutOfRange",
            value=format_number(number),
            node_id=node_id,
            low=format_number(range_["low"]),
            high=format_number(range_["high"]),
            unit=f" {info['unit']}" if info.get("unit") else "",
            source="the OPC UA server's own EURange",
        )
    )


def check_max_change(node_id: str, value: Any, limit: float, current: dict | None) -> None:
    present = current.get("number") if current else None
    if present is None:
        reason = (
            "it could not be read"
            if current is None
            else current["status"]
            if not current["good"]
            else "the node returned no usable value"
        )
        raise ApplicationRefusal(message("currentValueUnreadable", node_id=node_id, reason=reason))
    wanted = as_number(value)
    if wanted is None:
        raise ApplicationRefusal(
            message("valueNotComparable", node_id=node_id, value=json_text(value))
        )
    change = abs(wanted - present)
    if change > limit:
        raise ApplicationRefusal(
            message(
                "valueChangeTooLarge",
                node_id=node_id,
                current=format_number(present),
                value=format_number(wanted),
                change=format_number(change),
                limit=format_number(limit),
            )
        )


async def write_nodes(
    port: WritePort,
    nodes: list[dict],
    limits: dict[str, int],
    bounds: dict[int, float | None],
    allow_out_of_range: bool,
) -> list[dict]:
    if not nodes:
        raise ApplicationRefusal(message("emptyArray", tool="write_opcua_nodes", argument="nodes"))
    if len(nodes) > limits["write"]:
        raise ApplicationRefusal(
            message(
                "tooManyWritesForServer",
                tool="write_opcua_nodes",
                count=len(nodes),
                limit=limits["write"],
            )
        )
    try:
        results = [
            {
                "node_id": canonical_node_id(str(node.get("node_id", ""))),
                "status": "Good",
                "error": None,
            }
            for node in nodes
        ]
        indices = [
            index
            for index, node in enumerate(nodes)
            if not node.get("data_type") or bounds.get(index) is not None
        ]
        current = await port.current(nodes, indices, limits["read"]) if indices else {}
        node_ids = [str(node.get("node_id", "")) for node in nodes]
        engineering = {} if allow_out_of_range else await port.engineering(node_ids)
        for index, node in enumerate(nodes):
            value = node.get("value")
            for element in value if isinstance(value, list) else [value]:
                check_eu_range(node_ids[index], element, engineering.get(node_ids[index]))
            limit = bounds.get(index)
            if limit is not None:
                check_max_change(node_ids[index], value, limit, current.get(index))
        prepared = []
        for index, node in enumerate(nodes):
            failure = await port.prepare(node, index)
            if failure:
                results[index].update(failure)
            else:
                prepared.append(index)
        if prepared:
            statuses = await port.send()
            for index, status in zip(prepared, statuses, strict=True):
                results[index]["status"] = status
        return results
    except ApplicationRefusal:
        raise
    except Exception as error:
        raise AdapterFailure(
            "write", message("writeFailed", reason=describe_error(error)), error
        ) from error
