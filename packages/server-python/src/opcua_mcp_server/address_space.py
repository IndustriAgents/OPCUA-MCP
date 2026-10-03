"""Browsing the address space for answers rather than for records.

``browse_opcua_nodes`` walks the address space to *show* it. Everything here walks
it to *decide* something: whether a method belongs to an object, which node a
policy browse path names, what a ``writable_subtrees`` rule or a ``deny_read``
entry covers, and what a DataType is encoded as. Those answers feed checks that
refuse things, so the helpers are stricter than a listing has to be — every
browse result is status-checked and every continuation point is drained, because
a short answer read as a complete one is exactly the silent wrong answer a check
must not be built on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opcua import ua

from .contract import CONTRACT
from .limits import chunked
from .node_ids import canonical_node_id
from .operation_limits import browse_chunk

_TRAVERSAL = CONTRACT["traversal"]

#: The standard Root folder, which an absolute browse path is written from.
ROOT_FOLDER = "ns=0;i=84"

#: How many nodes one batched Browse names before a server's own
#: MaxNodesPerBrowse is taken into account. The same project cap the type
#: definition browse uses, for the same reason.
MAX_PER_BROWSE: int = _TRAVERSAL["maxTypeDefinitionsPerRequest"]

_HAS_SUBTYPE = ua.NodeId(ua.ObjectIds.HasSubtype)


def node_id_text(node_id: Any) -> str:
    """A NodeId or ExpandedNodeId as the canonical string everything compares by."""
    return canonical_node_id(node_id.to_string())


def browse_name_matches(segment: str, namespace_index: int, name: str) -> bool:
    """Whether a browse-path segment names this BrowseName.

    ``2:Sensors`` matches only namespace 2; a bare ``Sensors`` matches the name
    in whatever namespace it is in. The bare form is what someone types when
    they know what a thing is called and not which namespace it was loaded into
    — which is the entire reason ``browse_path`` exists.
    """
    prefix, separator, rest = segment.partition(":")
    if separator and prefix.isdigit():
        return int(prefix) == namespace_index and rest == name
    return segment == name


def _description(
    node_id: Any, direction: Any, reference_type: Any, include_subtypes: bool
) -> ua.BrowseDescription:
    description = ua.BrowseDescription()
    description.NodeId = node_id
    description.BrowseDirection = direction
    description.ReferenceTypeId = reference_type
    description.IncludeSubtypes = include_subtypes
    description.NodeClassMask = ua.NodeClass.Unspecified
    description.ResultMask = ua.BrowseResultMask.All
    return description


def _drain(client: Any, result: Any) -> tuple[Any, list[Any]]:
    """One node's whole answer: its status, and every reference across continuations.

    A continued result is status-checked like the first: a server that expires a
    continuation point answers with a Bad status and no references, and taking
    that as the end of the list would return a short answer as a complete one.
    """
    references = list(result.References)
    status = result.StatusCode
    point = result.ContinuationPoint
    while status.is_good() and point:
        params = ua.BrowseNextParameters()
        params.ContinuationPoints = [point]
        params.ReleaseContinuationPoints = False
        result = client.uaclient.browse_next(params)[0]
        status = result.StatusCode
        references.extend(result.References)
        point = result.ContinuationPoint
    return status, references


def browse_many(
    client: Any,
    node_ids: list[str],
    server_limits: dict[str, int | None],
    *,
    direction: Any = ua.BrowseDirection.Forward,
    reference_type: int = ua.ObjectIds.HierarchicalReferences,
    include_subtypes: bool = True,
) -> list[tuple[Any, list[Any]]]:
    """Browse several nodes at once: one (status, references) per node, in order.

    One Browse per chunk rather than one per node, so a walk costs a round trip
    per level instead of per node. A node the server refuses is its own Bad
    status in its own place, never an exception: what a refused node means — a
    gap, a dead end, a reason to stop — is the caller's to decide.
    """
    answers: list[tuple[Any, list[Any]]] = []
    for part in chunked(node_ids, browse_chunk(server_limits, MAX_PER_BROWSE)):
        params = ua.BrowseParameters()
        params.View.Timestamp = ua.get_win_epoch()
        params.NodesToBrowse = [
            _description(
                ua.NodeId.from_string(node_id),
                direction,
                ua.NodeId(reference_type),
                include_subtypes,
            )
            for node_id in part
        ]
        params.RequestedMaxReferencesPerNode = 0
        answers.extend(_drain(client, result) for result in client.uaclient.browse(params))
    return answers


def browse_references(
    client: Any,
    node_id: str,
    *,
    direction: Any = ua.BrowseDirection.Forward,
    reference_type: int = ua.ObjectIds.HierarchicalReferences,
) -> list[Any]:
    """Every reference of one node, raising on a Bad status rather than answering [].

    python-opcua's ``get_children`` and ``get_references`` never look at
    ``BrowseResult.StatusCode``, so a node the server refuses comes back as one
    with no references — which, for a check, reads as "the method is not
    there" when the truth is "nobody could look".
    """
    [(status, references)] = browse_many(
        client, [node_id], {}, direction=direction, reference_type=reference_type
    )
    if not status.is_good():
        raise ValueError(f"Browse failed with status: {status.name}")
    return references


def supertype_of(client: Any, data_type: str) -> str | None:
    """The supertype of one type node, or None at the top of its hierarchy.

    Every inverse reference, filtered here rather than by the server:
    python-opcua's own server answers a browse filtered to HasSubtype with nothing
    at all, and ``method-arguments.ts`` does the same for that reason.
    """
    references = client.get_node(data_type).get_references(direction=ua.BrowseDirection.Inverse)
    for reference in references:
        if reference.ReferenceTypeId == _HAS_SUBTYPE:
            return node_id_text(reference.NodeId)
    return None


def is_subtype_reference(reference: Any) -> bool:
    """Whether a reference is HasSubtype, compared as python-opcua compares NodeIds."""
    return reference.ReferenceTypeId == _HAS_SUBTYPE


#: What a supertype lookup is, for code that takes one as a parameter.
SupertypeOf = Callable[[str], "str | None"]


__all__ = [
    "MAX_PER_BROWSE",
    "ROOT_FOLDER",
    "SupertypeOf",
    "browse_many",
    "browse_name_matches",
    "browse_references",
    "is_subtype_reference",
    "node_id_text",
    "supertype_of",
]
