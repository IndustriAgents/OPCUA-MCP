"""Method orchestration over JSON-facing metadata and one native call."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id


class MethodPort(Protocol):
    async def input_types(self, method_node_id: str) -> list[dict]: ...
    async def call(
        self, object_node_id: str, method_node_id: str, arguments: list[dict]
    ) -> dict: ...


async def call_method(
    port: MethodPort, object_node_id: str, method_node_id: str, arguments: list[Any] | None = None
) -> dict:
    try:
        declared = await port.input_types(method_node_id)
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
