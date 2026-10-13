"""The information-model port: node facts, method ownership, interlock and alarm readings.

Native reads behind :class:`~..application.model.ModelPort`, on the facade API
both Python backends support. Facts and ownership come from the per-session
``NodeFacts`` cache, which never raises; the two reads made fresh for each call
(interlock values, a condition's source and severity) raise :class:`AdapterFailure`
with the native cause kept, so a dead session still takes the retry path.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opcua import ua

from ..address_space import is_node_id, node_id_text
from ..connection import is_connection_error
from ..errors import AdapterFailure, describe_error
from ..limits import chunked
from ..node_metadata import first_target, property_path
from ..operation_limits import read_chunk
from ..records import variant_to_json

#: The two properties of a condition its alarm scope is judged on, in the order
#: their browse paths are built: what raised it, and how severe it is.
_CONDITION_PROPERTIES = (("SourceNode", "0:SourceNode"), ("Severity", "0:Severity"))


def _read_values(client: Any, node_ids: list[Any], chunk: int) -> list[Any]:
    """One logical Read of the Value attribute, sent in chunks of at most ``chunk``."""
    return [
        value
        for part in chunked(node_ids, chunk)
        for value in client.uaclient.get_attributes(part, ua.AttributeIds.Value)
    ]


def _status(data_value: Any) -> str | None:
    """The name of a Bad or Uncertain status, or None for a Good one."""
    status = getattr(data_value, "StatusCode", None)
    return None if status is None or status.is_good() else str(status.name)


def _reading(data_value: Any) -> dict[str, Any]:
    """One interlock reading as ``check_preconditions`` takes it."""
    status = _status(data_value)
    if status is not None:
        return {"status": status}
    return {"status": "Good", "value": variant_to_json(data_value.Value)}


def _source(data_value: Any) -> tuple[str, Any]:
    status = _status(data_value)
    if status is not None:
        return "error", status
    value = getattr(getattr(data_value, "Value", None), "Value", None)
    return ("value", node_id_text(value)) if is_node_id(value) else ("error", "BadTypeMismatch")


def _severity(data_value: Any) -> tuple[str, Any]:
    status = _status(data_value)
    if status is not None:
        return "error", status
    value = getattr(getattr(data_value, "Value", None), "Value", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return "value", int(value)
    return "error", "BadTypeMismatch"


def condition_facts(client: Any, condition_id: str, server_limits: dict) -> dict[str, Any]:
    """A condition's SourceNode and Severity: one translate and one read.

    Each comes back as ``("value", …)``, or as ``("error", status)`` for what
    kept it from being read — a value of the wrong type is ``BadTypeMismatch``.
    """
    nodeid = client.get_node(condition_id).nodeid
    results = client.uaclient.translate_browsepaths_to_nodeids(
        [property_path(nodeid, path) for _, path in _CONDITION_PROPERTIES]
    )
    found: dict[str, tuple[str, Any]] = {}
    targets = []
    for (name, _), result in zip(_CONDITION_PROPERTIES, results, strict=True):
        target = first_target(result)
        if target is None:
            # A Good translate with no target names nothing either.
            status = result.StatusCode
            found[name] = ("error", "BadNoMatch" if status.is_good() else str(status.name))
        else:
            targets.append((name, target))
    values = _read_values(client, [target for _, target in targets], read_chunk(server_limits))
    for (name, _), data_value in zip(targets, values, strict=True):
        found[name] = _source(data_value) if name == "SourceNode" else _severity(data_value)
    return found


class PythonOpcuaModelPort:
    def __init__(self, client, node_facts, server_limits: dict):
        self.client = client
        self.node_facts = node_facts
        self.server_limits = server_limits

    async def facts(self, node_ids: list[str]) -> dict[str, dict | None]:
        return await asyncio.to_thread(self.node_facts.for_nodes, self.client, node_ids)

    async def on_object(self, object_node_id: str, method_node_id: str) -> bool | None:
        return await asyncio.to_thread(
            self.node_facts.on_object, self.client, object_node_id, method_node_id
        )

    async def readings(self, node_ids: list[str]) -> list[dict]:
        def read():
            try:
                ids = [self.client.get_node(node_id).nodeid for node_id in node_ids]
                values = _read_values(self.client, ids, read_chunk(self.server_limits))
                return [_reading(value) for value in values]
            except Exception as error:
                raise AdapterFailure("interlocks", describe_error(error), error) from error

        return await asyncio.to_thread(read) if node_ids else []

    async def condition(self, condition_id: str) -> dict[str, tuple[str, Any]]:
        def read():
            try:
                return condition_facts(self.client, condition_id, self.server_limits)
            except Exception as error:
                if is_connection_error(error):
                    raise AdapterFailure("alarm-scope", describe_error(error), error) from error
                # Unreadable is an answer: the scope refuses on it only where it
                # restricts what the property would have said.
                reason = describe_error(error)
                return {name: ("error", reason) for name, _ in _CONDITION_PROPERTIES}

        return await asyncio.to_thread(read)
