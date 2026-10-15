"""Write orchestration. A per-call port prepares values and sends at most one service."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id
from ..policy import as_number, format_number
from ..result_text import json_text
from ..write_plan import EU_RANGE_SOURCE, INSTRUMENT_RANGE_SOURCE, plan_write, range_refusal
from .model import Model


class WritePort(Protocol):
    async def facts(self, node_ids: list[str]) -> dict[str, dict | None]: ...
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


def _range_refusal(
    node_id: str, value: Any, engineering: dict | None, allow_out_of_range: bool
) -> str | None:
    """The refusal for a value outside the ranges the plant published, or None.

    ``plan_write`` checks the ranges only for a node whose facts were read; one
    whose facts were not is still held to whatever range it published, as every
    write was before the plans existed. The EURange unless the override lifts
    it, the InstrumentRange always. An array is checked element by element:
    writing [0, 9999] to a node whose range stops at 100 is writing 9999 to it.
    """
    info = engineering or {}
    eu_range = None if allow_out_of_range else info.get("eu_range")
    instrument_range = info.get("instrument_range")
    for element in value if isinstance(value, list) else [value]:
        number = as_number(element)
        if number is None:
            continue
        refusal = (
            eu_range and range_refusal(node_id, number, eu_range, info.get("unit"), EU_RANGE_SOURCE)
        ) or (
            instrument_range
            and range_refusal(
                node_id, number, instrument_range, info.get("unit"), INSTRUMENT_RANGE_SOURCE
            )
        )
        if refusal:
            return refusal
    return None


def plan_all(
    nodes: list[dict],
    node_ids: list[str],
    facts: dict[str, dict | None],
    engineering: dict[str, dict | None] | None,
    allow_out_of_range: bool,
) -> list[dict]:
    """Each node's plan (see write_plan.py), from what it publishes about itself."""
    plans = []
    for node_id, node in zip(node_ids, nodes, strict=True):
        known = facts.get(node_id)
        info = engineering.get(node_id) if engineering is not None else None
        plan = plan_write(
            {"node_id": node_id, "value": node.get("value"), "data_type": node.get("data_type")},
            known,
            info,
            {"allow_out_of_range": allow_out_of_range},
        )
        if plan["outcome"] == "send" and not (known and known.get("status") == "Good"):
            refusal = _range_refusal(node_id, plan["value"], info, allow_out_of_range)
            if refusal is not None:
                plan = {"outcome": "refuse", "error": refusal}
        plans.append(plan)
    return plans


async def write_nodes(
    port: WritePort,
    nodes: list[dict],
    limits: dict[str, int],
    bounds: dict[int, float | None],
    allow_out_of_range: bool,
    model: Model | None = None,
) -> list[dict]:
    """Plan each write, check it, then send the batch as one Write.

    Each write is planned from what its node says about itself before anything
    is sent: the type it is converted to comes from the node's DataType
    attribute, so a write-only node is writable without reading it, and only a
    node whose type is still unknown — or that carries a ``max_change`` bound,
    which is a bound on the *move* — has its current value read, in one read for
    both. A node that cannot take the write (an Object, a read-only node, a list
    for a scalar) is that node's record and nothing is sent to it; a value that
    is not one of its states, or is outside the range the plant published,
    refuses the whole batch, so it can never end up partially applied. Then,
    with ``model``, the operator's interlocks for the writes that will go out.
    """
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
        node_ids = [str(node.get("node_id", "")) for node in nodes]
        results = [
            {"node_id": canonical_node_id(node_id), "status": "Good", "error": None}
            for node_id in node_ids
        ]
        facts = await port.facts(node_ids)
        # A plan's type and shape never depend on the plant's ranges — only
        # whether it is refused does — so which nodes need their current value
        # is known before the engineering record is.
        indices = [
            index
            for index, plan in enumerate(plan_all(nodes, node_ids, facts, None, allow_out_of_range))
            if plan["outcome"] == "send"
            and (plan["data_type"] is None or bounds.get(index) is not None)
        ]
        current = await port.current(nodes, indices, limits["read"]) if indices else {}
        # Read even when the EURange check is lifted: the InstrumentRange is not.
        engineering_ids = [
            node_ids[index]
            for index, plan in enumerate(plan_all(nodes, node_ids, facts, None, allow_out_of_range))
            if plan["outcome"] == "send"
        ]
        engineering = await port.engineering(engineering_ids)
        plans = plan_all(nodes, node_ids, facts, engineering, allow_out_of_range)
        refusal = next((plan for plan in plans if plan["outcome"] == "refuse"), None)
        if refusal is not None:
            raise ApplicationRefusal(refusal["error"])
        sending = [index for index, plan in enumerate(plans) if plan["outcome"] == "send"]
        if model is not None:
            await model.check_write_interlocks([node_ids[index] for index in sending])
        # Against the value that will be written — a state's label is its number
        # by now — and, like every bound, for the whole batch.
        for index in sending:
            limit = bounds.get(index)
            if limit is not None:
                check_max_change(node_ids[index], plans[index]["value"], limit, current.get(index))
        prepared = []
        for index, plan in enumerate(plans):
            if plan["outcome"] == "skip":
                results[index].update(status=plan["status"], error=plan["error"])
                continue
            planned = {
                "node_id": node_ids[index],
                "value": plan["value"],
                "data_type": plan["data_type"],
                "array": plan["array"],
            }
            failure = await port.prepare(planned, index)
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
