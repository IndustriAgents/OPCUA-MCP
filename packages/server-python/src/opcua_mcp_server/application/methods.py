"""Method orchestration over JSON-facing metadata and one native call."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id
from ..write_plan import plan_call
from .model import Model


class MethodPort(Protocol):
    async def input_types(self, method_node_id: str) -> list[dict] | None: ...
    async def call(
        self, object_node_id: str, method_node_id: str, arguments: list[dict]
    ) -> dict: ...


async def check_call(
    model: Model,
    object_node_id: str,
    method_node_id: str,
    declared: list[dict] | None,
    arguments: list[Any] | None,
) -> None:
    """Refuse a call that cannot work, from what the two nodes publish, then its interlocks.

    Each fact that cannot be read skips its check; the server still answers for
    itself. ``declared`` None (no InputArguments published) is not ``[]`` (none
    taken): only the second can have its arguments counted.
    """
    facts = await model.port.facts([object_node_id, method_node_id])
    refusal = plan_call(
        {
            "object_node_id": object_node_id,
            "method_node_id": method_node_id,
            "object_facts": facts.get(object_node_id),
            "method_facts": facts.get(method_node_id),
            "declared_arguments": None if declared is None else len(declared),
            "given_arguments": len(arguments or []),
            "on_object": await model.port.on_object(object_node_id, method_node_id),
        }
    )
    if refusal is not None:
        raise ApplicationRefusal(refusal)
    await model.check_call_interlocks(object_node_id, method_node_id)


async def call_method(
    port: MethodPort,
    object_node_id: str,
    method_node_id: str,
    arguments: list[Any] | None = None,
    model: Model | None = None,
) -> dict:
    """Call a method with its arguments typed as declared; with ``model``, checked first."""
    try:
        declared = await port.input_types(method_node_id)
        if model is not None:
            await check_call(model, object_node_id, method_node_id, declared, arguments)
        declared = declared or []
        typed = [
            {
                "value": value,
                "dataType": declared[index]["dataType"] if index < len(declared) else None,
                "isArray": declared[index]["isArray"] if index < len(declared) else False,
            }
            for index, value in enumerate(arguments or [])
        ]
        result = await port.call(object_node_id, method_node_id, typed)
        return {
            "object_node_id": canonical_node_id(object_node_id),
            "method_node_id": canonical_node_id(method_node_id),
            "status": result["status"],
            "outputs": result["outputs"],
        }
    except ApplicationRefusal:
        raise
    except Exception as error:
        raise AdapterFailure(
            "method",
            message(
                "methodFailed",
                method_node_id=method_node_id,
                object_node_id=object_node_id,
                reason=describe_error(error),
            ),
            error,
        ) from error
