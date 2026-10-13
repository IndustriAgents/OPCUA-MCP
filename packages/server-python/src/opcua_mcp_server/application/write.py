"""Write orchestration. A per-call port prepares values and sends at most one service."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id
from ..policy import as_number, format_number
from ..result_text import json_text
from ..write_plan import plan_write
from .model import Model


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


async def plan_writes(
    port: WritePort,
    model: Model,
    nodes: list[dict],
    node_ids: list[str],
    allow_out_of_range: bool,
) -> list[dict]:
    """Each node's plan (see write_plan.py), from what it publishes about itself.

    The engineering record even under OPCUA_ALLOW_OUT_OF_RANGE_WRITES: that lifts
    the EURange, and the InstrumentRange still holds. Neither read is ever the
    reason a write fails. A refusal is raised here, before anything is sent, so
    the batch can never end up partially applied.
    """
    facts = await model.port.facts(node_ids)
    engineering = await port.engineering(node_ids)
    options = {"allow_out_of_range": allow_out_of_range}
    plans = [
        plan_write(
            {"node_id": node_id, "value": node.get("value"), "data_type": node.get("data_type")},
            facts.get(node_id),
            engineering.get(node_id),
            options,
        )
        for node_id, node in zip(node_ids, nodes, strict=True)
    ]
    refusal = next((plan for plan in plans if plan["outcome"] == "refuse"), None)
    if refusal is not None:
        raise ApplicationRefusal(refusal["error"])
    return plans


def _planned(node: dict, plan: dict | None) -> dict:
    """The node as it is to be sent: its plan's value, type and shape where there is one.

    The resolved value, so a state's label has become its number; ``data_type``
    from the DataType attribute, or None where the type is still to be read off
    the current value; ``array`` from the ValueRank, or None where it says neither.
    """
    if plan is None:
        return node
    return {
        "node_id": node.get("node_id"),
        "value": plan["value"],
        "data_type": plan["data_type"],
        "array": plan["array"],
    }


async def write_nodes(
    port: WritePort,
    nodes: list[dict],
    limits: dict[str, int],
    bounds: dict[int, float | None],
    allow_out_of_range: bool,
    model: Model | None = None,
) -> list[dict]:
    """Write a batch as one service call, after every check that needs no write.

    With ``model``, each node is first planned from its own attributes: a node
    that cannot take the write at all (read-only, not a Variable, a list for a
    scalar) is reported in its own record and not sent; a value outside what it
    allows refuses the batch; and the interlocks on what is left must hold.
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
        plans: list[dict | None] = [None] * len(nodes)
        if model is not None:
            plans = await plan_writes(port, model, nodes, node_ids, allow_out_of_range)
            for index, plan in enumerate(plans):
                if plan["outcome"] == "skip":
                    results[index].update(status=plan["status"], error=plan["error"])
            await model.check_write_interlocks(
                [node_ids[index] for index, plan in enumerate(plans) if plan["outcome"] == "send"]
            )
        sending = {
            index: _planned(node, plans[index])
            for index, node in enumerate(nodes)
            if plans[index] is None or plans[index]["outcome"] == "send"
        }
        # A node needs its current value read for either of two reasons: its type
        # is not known yet and has to be inferred, or it carries a `max_change`
        # bound, which is a bound on the *move*. One read covers both.
        indices = [
            index
            for index, node in sending.items()
            if not node.get("data_type") or bounds.get(index) is not None
        ]
        current = await port.current(nodes, indices, limits["read"]) if indices else {}
        # A planned write had its ranges checked by its plan, the InstrumentRange
        # beside the EURange; an unplanned one is checked against the EURange here.
        engineering = (
            {} if model is not None or allow_out_of_range else await port.engineering(node_ids)
        )
        for index, node in sending.items():
            value = node.get("value")
            if model is None:
                # An array write is checked element by element. Writing [0, 9999]
                # to a node whose range stops at 100 is writing 9999 to it.
                for element in value if isinstance(value, list) else [value]:
                    check_eu_range(node_ids[index], element, engineering.get(node_ids[index]))
            limit = bounds.get(index)
            if limit is not None:
                check_max_change(node_ids[index], value, limit, current.get(index))
        prepared = []
        for index, node in sending.items():
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
