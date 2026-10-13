"""Native python-opcua browse services and codecs, bound to one selected client."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from opcua import Node, ua

from ..application.browse import BrowsePort
from ..contract import CONTRACT
from ..errors import AdapterFailure, describe_error
from ..limits import chunked
from ..node_ids import canonical_node_id
from ..operation_limits import browse_chunk, read_chunk
from ..records import variant_to_json
from .opcua_read import _data_type_name

_TRAVERSAL = CONTRACT["traversal"]


def browse_children(node: Node) -> list[Node]:
    """Browse a node's references, failing on a bad browse status.

    python-opcua's ``get_children()`` never looks at ``BrowseResult.StatusCode``,
    so a node the server refuses comes back as an empty child list —
    indistinguishable from a node that really has none, and a *successful* result
    besides.

    Otherwise a faithful copy of what ``get_children()`` asks for, which is not
    what ``get_references()`` defaults to: *hierarchical* references, *forward*
    only. Browsing ``References``/``Both`` instead — the ``get_references()``
    defaults — walks back up to the parent and out to the type definition, so
    ``ns=2;i=1`` answers ``0:Objects`` and ``0:FolderType`` rather than its own
    ``2:Sensors``.
    """
    description = ua.BrowseDescription()
    description.NodeId = node.nodeid
    description.BrowseDirection = ua.BrowseDirection.Forward
    description.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    description.IncludeSubtypes = True
    description.NodeClassMask = ua.NodeClass.Unspecified
    description.ResultMask = ua.BrowseResultMask.All

    params = ua.BrowseParameters()
    params.View.Timestamp = ua.get_win_epoch()
    params.NodesToBrowse.append(description)
    params.RequestedMaxReferencesPerNode = 0

    # A server may cap how many references one response carries whatever we ask
    # for, so drain the continuation point as `get_references()` does — otherwise
    # a large node silently browses short. Every result is status-checked, the
    # continued ones included: a server that expires or refuses a continuation
    # point answers with a bad status and no references, which unchecked would
    # end the loop and return a *truncated* child list as a success — the same
    # class of silent wrong answer this function exists to stop.
    references = []
    results = node.server.browse(params)
    while True:
        result = results[0]
        if not result.StatusCode.is_good():
            # `.name`, not the whole StatusCode: node-opcua renders the same
            # rejection as `BadNodeIdUnknown (0x80340000)` and python-opcua as
            # `StatusCode(BadNodeIdUnknown)`. Neither server controls the other's
            # spelling, but both can name the status plainly.
            raise ValueError(f"Browse failed with status: {result.StatusCode.name}")

        references.extend(result.References)
        if not result.ContinuationPoint:
            break

        next_params = ua.BrowseNextParameters()
        next_params.ContinuationPoints = [result.ContinuationPoint]
        next_params.ReleaseContinuationPoints = False
        results = node.server.browse_next(next_params)

    return references


def _read_values(client: Any, node_ids: list[Any], attribute: Any, chunk: int) -> list[Any]:
    """One logical read, sent as consecutive Reads of at most ``chunk`` nodes.

    Sequential rather than in parallel: the chunking exists because the server
    said how much one request may carry, and firing every chunk at once would
    put the same load on it in a different envelope. The results are
    concatenated in the order asked, so each node keeps its own status in its
    own place (issue #139).
    """
    return [
        value
        for part in chunked(node_ids, chunk)
        for value in client.uaclient.get_attributes(part, attribute)
    ]


def _describe_node(client, node_id: str, parent_node_id: str) -> dict:
    """The record for one node read directly, rather than off a browse reference."""
    node = client.get_node(node_id)
    browse_name = node.get_browse_name()
    node_class = node.get_node_class()
    return {
        "node_id": canonical_node_id(node_id),
        "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
        "node_class": node_class.name,
        "parent_node_id": canonical_node_id(parent_node_id),
        "data_type": None,
        "value": None,
        "description": None,
        "type_definition": None,
    }


def type_definition_of(is_good: bool, browse_names: list[str]) -> str | None:
    """Which of a node's HasTypeDefinition references to report, if any.

    Split out from the browse and driven by ``tests/fixtures/type-definitions.json``
    because ``type-definitions.test.mjs`` has to answer identically: two clients
    browsing the same server must not disagree about what its nodes are.

    Exactly one, or nothing. OPC UA Part 3 §4.3 gives an Object or a Variable
    exactly one HasTypeDefinition, so:

    * none — a Method, a View or a type itself. That is an answer, not a failure.
    * two — a server no client can read correctly. Taking whichever came first
      would let the two runtimes report different types for the same node
      depending on how each library ordered the references, and would report the
      *base* type for a node that also declared a useful one. Saying nothing is
      the only answer that is both deterministic and never wrong.
    """
    if not is_good or len(browse_names) != 1:
        return None
    return browse_names[0] or None


def _fill_type_definitions(client, records: list[dict], server_limits: dict) -> None:
    """Fill in ``type_definition`` for ``records``, in one batched browse.

    ``HasTypeDefinition`` is non-hierarchical, so the traversal's own browse —
    forward hierarchical references only, deliberately, or every node would
    answer with its parent and its type instead of its children — never sees it.
    It takes a second browse, and that is why this is one request for the whole
    result rather than one per node: a 500-node walk would otherwise cost 500
    extra round trips to say what one already could.

    Best-effort, like the variable detail: a server that refuses this leaves the
    field null rather than failing a browse that succeeded.
    """
    if not records:
        return
    descriptions = []
    for record in records:
        description = ua.BrowseDescription()
        description.NodeId = ua.NodeId.from_string(record["node_id"])
        description.BrowseDirection = ua.BrowseDirection.Forward
        description.ReferenceTypeId = ua.NodeId.from_string(_TRAVERSAL["hasTypeDefinitionNodeId"])
        # No subtypes: HasTypeDefinition has none, and asking for them would let
        # an unrelated reference through on a server that has invented one.
        description.IncludeSubtypes = False
        description.NodeClassMask = ua.NodeClass.Unspecified
        description.ResultMask = ua.BrowseResultMask.All
        descriptions.append(description)

    # Chunked for the same reason the property reads are: MaxNodesPerBrowse is
    # an operational limit a conformant server may enforce, and the default walk
    # already returns up to 500 nodes. A server that states a lower one gets
    # smaller chunks.
    size = browse_chunk(server_limits, _TRAVERSAL["maxTypeDefinitionsPerRequest"])
    for start in range(0, len(descriptions), size):
        chunk = descriptions[start : start + size]
        params = ua.BrowseParameters()
        params.View.Timestamp = ua.get_win_epoch()
        params.NodesToBrowse = chunk
        params.RequestedMaxReferencesPerNode = 0
        try:
            results = client.uaclient.browse(params)
        except Exception:
            return
        for record, result in zip(records[start : start + size], results, strict=False):
            record["type_definition"] = type_definition_of(
                result.StatusCode.is_good(),
                [reference.BrowseName.Name for reference in result.References],
            )


def _fill_variable_detail(client, records: list[dict], server_limits: dict) -> None:
    """Fill in value, data type and description for the Variables among ``records``.

    One batched read of each attribute rather than three reads per node: a
    500-node inventory is otherwise 1500 round trips, which is the difference
    between a tool that answers and one that times out on real equipment.
    """
    variables = [record for record in records if record["node_class"] == "Variable"]
    if not variables:
        return
    node_ids = [client.get_node(record["node_id"]).nodeid for record in variables]
    chunk = read_chunk(server_limits)
    try:
        values = _read_values(client, node_ids, ua.AttributeIds.Value, chunk)
        data_types = _read_values(client, node_ids, ua.AttributeIds.DataType, chunk)
        descriptions = _read_values(client, node_ids, ua.AttributeIds.Description, chunk)
    except Exception:
        # Best-effort enrichment: the nodes were found, and reporting them
        # without their values beats failing a browse that succeeded.
        return

    for record, data_value, data_type, description in zip(
        variables, values, data_types, descriptions, strict=True
    ):
        if data_value.StatusCode.is_good():
            record["value"] = variant_to_json(data_value.Value)
            record["data_type"] = _data_type_name(data_value.Value)
        if record["data_type"] is None and data_type.StatusCode.is_good():
            identifier = getattr(getattr(data_type, "Value", None), "Value", None)
            if isinstance(identifier, ua.NodeId) and identifier.NamespaceIndex == 0:
                with contextlib.suppress(ValueError):
                    record["data_type"] = ua.VariantType(identifier.Identifier).name
        text = getattr(getattr(description, "Value", None), "Value", None)
        text = getattr(text, "Text", None)
        record["description"] = text if text else None


class PythonOpcuaBrowsePort(BrowsePort):
    def __init__(self, client, server_limits: dict):
        self.client = client
        self.server_limits = server_limits

    async def _run(self, operation, *args):
        def invoke():
            try:
                return operation(*args)
            except Exception as error:
                raise AdapterFailure("browse", describe_error(error), error) from error

        return await asyncio.to_thread(invoke)

    async def children(self, node_id: str) -> list[dict]:
        def children():
            refs = browse_children(self.client.get_node(node_id))
            return [
                {
                    "nodeId": canonical_node_id(ref.NodeId.to_string()),
                    "namespaceIndex": ref.BrowseName.NamespaceIndex,
                    "name": ref.BrowseName.Name,
                    "nodeClass": ref.NodeClass.name,
                }
                for ref in refs
            ]

        return await self._run(children)

    async def describe(self, node_id: str, parent_node_id: str) -> dict:
        return await self._run(_describe_node, self.client, node_id, parent_node_id)

    async def enrich(self, records: list[dict], include_values: bool) -> None:
        def enrich():
            _fill_type_definitions(self.client, records, self.server_limits)
            if include_values:
                _fill_variable_detail(self.client, records, self.server_limits)

        await self._run(enrich)
