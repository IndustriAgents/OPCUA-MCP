"""A small in-memory OPC UA address space behind a python-opcua-shaped client.

Enough of ``Client`` and ``UaClient`` for the code that *reads the information
model* — node facts, policy browse paths, subtree and deny walks — to run against
something whose answers a test controls, and to count what it asked: how many
Reads, how large, how many translates and browses. A live server would answer
too, but it cannot be made to hold two children with one name, refuse a
continuation point, or say how many requests it was sent.

Not collected (no ``test_`` prefix); imported by the tests that need it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opcua import ua
from opcua_mcp_server.node_ids import canonical_node_id

HIERARCHICAL = {
    ua.ObjectIds.Organizes,
    ua.ObjectIds.HasComponent,
    ua.ObjectIds.HasProperty,
    ua.ObjectIds.HasOrderedComponent,
}

_ATTRIBUTE_FIELDS = {
    ua.AttributeIds.NodeClass: "node_class",
    ua.AttributeIds.DataType: "data_type",
    ua.AttributeIds.ValueRank: "value_rank",
    ua.AttributeIds.ArrayDimensions: "array_dimensions",
    ua.AttributeIds.AccessLevel: "access_level",
    ua.AttributeIds.UserAccessLevel: "user_access_level",
    ua.AttributeIds.Executable: "executable",
    ua.AttributeIds.UserExecutable: "user_executable",
    ua.AttributeIds.Value: "value",
}


def _status(name: str) -> ua.StatusCode:
    return ua.StatusCode(getattr(ua.StatusCodes, name))


@dataclass
class FakeNode:
    node_id: str
    browse_name: tuple[int, str]
    node_class: ua.NodeClass
    attributes: dict[str, Any] = field(default_factory=dict)
    type_definition: str | None = None


@dataclass
class _Reference:
    source: str
    reference_type: int
    target: str


class FakeAddressSpace:
    """Nodes, references between them, and a log of every request made."""

    def __init__(self) -> None:
        self.nodes: dict[str, FakeNode] = {}
        self.references: list[_Reference] = []
        self.read_sizes: list[int] = []
        self.translate_sizes: list[int] = []
        self.browse_sizes: list[int] = []
        self.supertype_lookups: list[str] = []
        #: Request kind ("read", "translate", "browse", "references") -> the
        #: exception to raise instead of answering.
        self.fail: dict[str, BaseException] = {}
        #: Node ids whose browse answers with this status.
        self.browse_status: dict[str, str] = {}
        #: References per browse page, to make continuation points happen.
        self.page_size: int | None = None
        self.root = self.add("ns=0;i=84", (0, "Root"), ua.NodeClass.Object)
        self.add("ns=0;i=85", (0, "Objects"), ua.NodeClass.Object, parent="ns=0;i=84")

    def add(
        self,
        node_id: str,
        browse_name: tuple[int, str],
        node_class: ua.NodeClass,
        *,
        parent: str | None = None,
        reference: int = ua.ObjectIds.Organizes,
        type_definition: str | None = None,
        **attributes: Any,
    ) -> str:
        node_id = canonical_node_id(node_id)
        self.nodes[node_id] = FakeNode(
            node_id, browse_name, node_class, dict(attributes), type_definition
        )
        if parent is not None:
            self.link(parent, reference, node_id)
        if type_definition is not None:
            self.link(node_id, ua.ObjectIds.HasTypeDefinition, type_definition)
        return node_id

    def link(self, source: str, reference_type: int, target: str) -> None:
        self.references.append(
            _Reference(canonical_node_id(source), reference_type, canonical_node_id(target))
        )

    def variable(self, node_id: str, name: str, parent: str, **attributes: Any) -> str:
        """A writable Double variable unless the attributes say otherwise."""
        values = {
            "data_type": "ns=0;i=11",
            "value_rank": -1,
            "access_level": 3,
            "user_access_level": 3,
            "value": 0.0,
        }
        values.update(attributes)
        return self.add(
            node_id,
            (2, name),
            ua.NodeClass.Variable,
            parent=parent,
            reference=ua.ObjectIds.HasComponent,
            type_definition=values.pop("type_definition", "ns=0;i=63"),
            **values,
        )

    def property(self, owner: str, node_id: str, name: str, value: Any) -> str:
        return self.add(
            node_id,
            (0, name),
            ua.NodeClass.Variable,
            parent=owner,
            reference=ua.ObjectIds.HasProperty,
            type_definition="ns=0;i=68",
            value=value,
            data_type="ns=0;i=21",
            value_rank=-1,
            access_level=1,
            user_access_level=1,
        )

    def client(self) -> FakeClient:
        return FakeClient(self)

    # --- what the services answer ------------------------------------------------

    def _forward(self, node_id: str, reference_types: set[int] | None) -> list[_Reference]:
        return [
            reference
            for reference in self.references
            if reference.source == node_id
            and (reference_types is None or reference.reference_type in reference_types)
        ]

    def _inverse(self, node_id: str, reference_types: set[int] | None) -> list[_Reference]:
        return [
            reference
            for reference in self.references
            if reference.target == node_id
            and (reference_types is None or reference.reference_type in reference_types)
        ]

    def describe(self, node_id: str, reference_type: int, forward: bool) -> ua.ReferenceDescription:
        node = self.nodes.get(node_id)
        description = ua.ReferenceDescription()
        description.ReferenceTypeId = ua.NodeId(reference_type)
        description.IsForward = forward
        description.NodeId = ua.NodeId.from_string(node_id)
        if node is not None:
            description.BrowseName = ua.QualifiedName(node.browse_name[1], node.browse_name[0])
            description.NodeClass = node.node_class
            if node.type_definition is not None:
                description.TypeDefinition = ua.NodeId.from_string(node.type_definition)
        return description


class _UaClient:
    def __init__(self, space: FakeAddressSpace) -> None:
        self._space = space
        self._pages: dict[bytes, list[ua.ReferenceDescription]] = {}

    def _answer(self, node_id: Any, attribute: int) -> ua.DataValue:
        node = self._space.nodes.get(canonical_node_id(node_id.to_string()))
        if node is None:
            value = ua.DataValue()
            value.StatusCode = _status("BadNodeIdUnknown")
            return value
        name = _ATTRIBUTE_FIELDS.get(attribute)
        if attribute == ua.AttributeIds.NodeClass:
            return ua.DataValue(ua.Variant(node.node_class, ua.VariantType.Int32))
        if name not in node.attributes:
            value = ua.DataValue()
            value.StatusCode = _status("BadAttributeIdInvalid")
            return value
        raw = node.attributes[name]
        if name == "data_type":
            raw = ua.NodeId.from_string(raw)
        if isinstance(raw, ua.DataValue):
            return raw
        if name == "array_dimensions":
            # Typed: an empty list says nothing python-opcua could guess a type from.
            return ua.DataValue(ua.Variant(raw, ua.VariantType.UInt32))
        return ua.DataValue(ua.Variant(raw))

    def read(self, parameters: ua.ReadParameters) -> list[ua.DataValue]:
        if "read" in self._space.fail:
            raise self._space.fail["read"]
        self._space.read_sizes.append(len(parameters.NodesToRead))
        return [self._answer(item.NodeId, item.AttributeId) for item in parameters.NodesToRead]

    def get_attributes(self, nodes: list[Any], attribute: int) -> list[ua.DataValue]:
        if "read" in self._space.fail:
            raise self._space.fail["read"]
        self._space.read_sizes.append(len(nodes))
        return [self._answer(node, attribute) for node in nodes]

    def translate_browsepaths_to_nodeids(self, paths: list[ua.BrowsePath]) -> list[Any]:
        if "translate" in self._space.fail:
            raise self._space.fail["translate"]
        self._space.translate_sizes.append(len(paths))
        results = []
        for path in paths:
            current = canonical_node_id(path.StartingNode.to_string())
            for element in path.RelativePath.Elements:
                wanted = (element.TargetName.NamespaceIndex, element.TargetName.Name)
                current = next(
                    (
                        reference.target
                        for reference in self._space._forward(current, HIERARCHICAL)
                        if self._space.nodes[reference.target].browse_name == wanted
                    ),
                    None,
                )
                if current is None:
                    break
            result = ua.BrowsePathResult()
            if current is None:
                result.StatusCode = _status("BadNoMatch")
            else:
                target = ua.BrowsePathTarget()
                target.TargetId = ua.NodeId.from_string(current)
                result.Targets = [target]
            results.append(result)
        return results

    def browse(self, parameters: ua.BrowseParameters) -> list[ua.BrowseResult]:
        if "browse" in self._space.fail:
            raise self._space.fail["browse"]
        self._space.browse_sizes.append(len(parameters.NodesToBrowse))
        return [self._browse_one(description) for description in parameters.NodesToBrowse]

    def _browse_one(self, description: ua.BrowseDescription) -> ua.BrowseResult:
        node_id = canonical_node_id(description.NodeId.to_string())
        result = ua.BrowseResult()
        if node_id in self._space.browse_status:
            result.StatusCode = _status(self._space.browse_status[node_id])
            return result
        if node_id not in self._space.nodes:
            result.StatusCode = _status("BadNodeIdUnknown")
            return result
        reference_type = description.ReferenceTypeId.Identifier
        kinds: set[int] | None = {reference_type}
        if reference_type == ua.ObjectIds.References:
            kinds = None
        elif reference_type == ua.ObjectIds.HierarchicalReferences:
            kinds = HIERARCHICAL
        found = []
        if description.BrowseDirection in (ua.BrowseDirection.Forward, ua.BrowseDirection.Both):
            found += [
                self._space.describe(reference.target, reference.reference_type, True)
                for reference in self._space._forward(node_id, kinds)
            ]
        if description.BrowseDirection in (ua.BrowseDirection.Inverse, ua.BrowseDirection.Both):
            found += [
                self._space.describe(reference.source, reference.reference_type, False)
                for reference in self._space._inverse(node_id, kinds)
            ]
        return self._page(result, found)

    def _page(self, result: ua.BrowseResult, found: list) -> ua.BrowseResult:
        size = self._space.page_size
        if size is None or len(found) <= size:
            result.References = found
            return result
        result.References = found[:size]
        point = f"cp-{len(self._pages)}".encode()
        self._pages[point] = found[size:]
        result.ContinuationPoint = point
        return result

    def browse_next(self, parameters: ua.BrowseNextParameters) -> list[ua.BrowseResult]:
        point = parameters.ContinuationPoints[0]
        return [self._page(ua.BrowseResult(), self._pages.pop(point))]


class _ClientNode:
    def __init__(self, space: FakeAddressSpace, node_id: str) -> None:
        self._space = space
        self.nodeid = ua.NodeId.from_string(node_id)

    def get_references(self, direction: Any = ua.BrowseDirection.Both) -> list:
        if "references" in self._space.fail:
            raise self._space.fail["references"]
        node_id = canonical_node_id(self.nodeid.to_string())
        self._space.supertype_lookups.append(node_id)
        return [
            self._space.describe(reference.source, reference.reference_type, False)
            for reference in self._space._inverse(node_id, None)
        ]


class FakeClient:
    """The two halves of python-opcua's ``Client`` the code under test reaches for."""

    def __init__(self, space: FakeAddressSpace) -> None:
        self._space = space
        self.uaclient = _UaClient(space)

    def get_node(self, node_id: str) -> _ClientNode:
        return _ClientNode(self._space, canonical_node_id(node_id))
