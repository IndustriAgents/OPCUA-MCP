"""Native python-opcua method metadata, variants and result codecs."""

from __future__ import annotations

import asyncio
from typing import Any

from opcua import ua

from ..errors import AdapterFailure, ApplicationRefusal, describe_error
from ..limits import LimitExceeded
from ..method_arguments import built_in_type, guess_variant
from ..node_ids import canonical_node_id
from ..records import variant_to_json
from ..variant_codec import convert_for_variant


def _input_argument_types(client, method_node_id: str) -> list[tuple[Any, bool]]:
    """The declared type of each input argument, or [] when the method publishes none.

    A declared DataType that is not itself built in (``Duration``, ``UtcTime``,
    an enumeration) is resolved to the built-in type it is encoded as, and one
    that resolves to none raises: that is a method whose argument cannot be
    encoded, not one that declares nothing, and guessing would send it anyway.
    """
    try:
        arguments = client.get_node(method_node_id).get_child(["0:InputArguments"]).get_value()
    except Exception:
        # Not every method publishes InputArguments, and a method with no
        # arguments has nothing to publish. Fall back rather than refuse.
        return []

    def supertype_of(data_type: str) -> str | None:
        # Every inverse reference, filtered here rather than by the server:
        # python-opcua's own server answers a browse filtered to HasSubtype with
        # nothing at all, and `method-arguments.ts` does the same for that reason.
        references = client.get_node(data_type).get_references(direction=ua.BrowseDirection.Inverse)
        for reference in references:
            if canonical_node_id(reference.ReferenceTypeId.to_string()) == canonical_node_id(
                ua.NodeId(ua.ObjectIds.HasSubtype).to_string()
            ):
                return canonical_node_id(reference.NodeId.to_string())
        return None

    return [
        (
            built_in_type(canonical_node_id(argument.DataType.to_string()), supertype_of),
            argument.ValueRank >= 1,
        )
        for argument in arguments or []
    ]


def _call(node: Any, method_node: Any, arguments: list[Any]) -> Any:
    """Call a method and return its whole CallMethodResult.

    Not python-opcua's ``call_method``, which returns only the outputs — so the
    tool reported ``"Good"`` whatever the server said — and flattens a single
    array output into what look like several outputs.
    """
    request = ua.CallMethodRequest()
    request.ObjectId = node.nodeid
    request.MethodId = method_node.nodeid
    request.InputArguments = arguments
    result = node.server.call([request])[0]
    # Good *severity*, not plain Good: GoodClamped or GoodLocalOverride is a call
    # that happened, and refusing it would report as failed an action the plant
    # carried out. The subcode is reported in `status` instead.
    if not result.StatusCode.is_good():
        raise ValueError(f"Method call failed with status: {result.StatusCode.name}")
    return result


class PythonOpcuaMethodPort:
    def __init__(self, client):
        self.client = client

    async def _run(self, operation):
        def invoke():
            try:
                return operation()
            except LimitExceeded as error:
                raise ApplicationRefusal(str(error)) from error
            except Exception as error:
                raise AdapterFailure("method", describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def input_types(self, method_node_id: str) -> list[dict]:
        def metadata():
            return [
                {"dataType": kind.name, "isArray": is_array}
                for kind, is_array in _input_argument_types(self.client, method_node_id)
            ]

        return await self._run(metadata)

    async def call(self, object_node_id: str, method_node_id: str, arguments: list[dict]) -> dict:
        def call():
            encoded = []
            for index, argument in enumerate(arguments):
                if argument["dataType"] is None:
                    encoded.append(guess_variant(argument["value"], index))
                else:
                    kind = getattr(ua.VariantType, argument["dataType"])
                    encoded.append(
                        ua.Variant(
                            convert_for_variant(argument["value"], kind, argument["isArray"]), kind
                        )
                    )
            result = _call(
                self.client.get_node(object_node_id), self.client.get_node(method_node_id), encoded
            )
            return {
                "status": str(result.StatusCode.name),
                "outputs": [variant_to_json(output) for output in result.OutputArguments],
            }

        return await self._run(call)
