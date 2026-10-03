"""What a node's own attributes say about writing to it, or calling it.

``node-facts.ts`` is the Node half and must produce the same record from the same
address space: the shared tables in ``tests/fixtures/`` (``write-plan.json``,
``method-plan.json``, ``write-access.json``, ``policy-check.json``) take this
record as their input, so a field one runtime fills and the other leaves null is
a check that runs on one server and not the other.

Every write used to be judged by reading the node's *current value* and copying
its type. That cannot tell a read-only node from a writable one, an Object from a
Variable, an array from a scalar or an enumeration's states from bare numbers —
and it fails outright on a node that can be written but not read, because reading
it is exactly what such a node refuses. The attributes say all of that, and every
server publishes them: NodeClass, DataType, ValueRank, ArrayDimensions,
AccessLevel, UserAccessLevel, and Executable/UserExecutable for a method.

Three rounds, then nothing
--------------------------

1. One Read of the eight attributes for every uncached node (chunked by the
   server's MaxNodesPerRead).
2. Each distinct DataType walked up to the built-in type it is encoded as
   (:func:`resolve_data_type`), once per session per DataType.
3. For an enumerated, integer or Boolean Variable, its ``EnumStrings``,
   ``EnumValues``, ``TrueState`` and ``FalseState`` properties — and an
   enumerated DataType's own ``EnumStrings``/``EnumValues`` — in one
   TranslateBrowsePathsToNodeIds and one Read, the way ``node_metadata`` fetches
   the engineering properties.

So a cold call costs a few round trips whatever the batch size, and a warm one
costs none. The cache is per session and dropped when the session is replaced
(:meth:`NodeFacts.forget`), for the same reason ``node_metadata``'s is.

The plant model only ever narrows
---------------------------------

Nothing here can grant anything: a fact is used to refuse a write the server
would refuse anyway, or to say so earlier and better. A fact that cannot be read
is ``None``, and a check whose fact is ``None`` is skipped — the OPC UA server
enforces its own access rights and answers for itself. And this is never the
reason a tool call fails: a failure is logged to stderr and the affected nodes
get ``None`` facts. A connection error is not swallowed so much as left for the
operation that follows, which hits the same dead session and takes the existing
retry and reconnect path. Failures are not cached, except a server that cannot
translate browse paths at all, which will not learn to.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from typing import Any

from opcua import ua

from .address_space import browse_references, node_id_text, supertype_of
from .connection import describe_error, is_connection_error
from .contract import CONTRACT
from .limits import chunked
from .node_ids import canonical_node_id
from .node_metadata import MAX_PER_REQUEST, first_target, property_path
from .operation_limits import UNSTATED, read_chunk, translate_chunk

#: The fifteen ``data_type`` spellings ``write_opcua_nodes`` accepts, from its own
#: input schema. A DataType that resolves to a built-in outside them — NodeId,
#: LocalizedText, StatusCode — is reported as null: the write then converts to the
#: current value's type, as it always did.
WRITE_DATA_TYPES: frozenset[str] = frozenset(
    next(tool for tool in CONTRACT["tools"] if tool["name"] == "write_opcua_nodes")["inputSchema"][
        "properties"
    ]["nodes"]["items"]["properties"]["data_type"]["enum"]
)

#: The DataTypes whose Variables can carry enumeration states or two-state
#: labels: Boolean and the eight integer types (Part 8 §5.3.3).
STATE_DATA_TYPES: frozenset[str] = frozenset(
    {"Boolean", "SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"}
)

#: Enumeration (Part 3 §8.14): every enumerated DataType is an Int32 on the wire.
_ENUMERATION = 29
#: The last ns=0 identifier that *is* a built-in type with a concrete encoding
#: (LocalizedText). 22 to 25 are Structure, DataValue, BaseDataType and
#: DiagnosticInfo, and 26 to 28 Number, Integer and UInteger — abstract here.
_LAST_CONCRETE = 21
_LAST_ABSTRACT = 28
#: Far deeper than any real type hierarchy; bounds a server whose HasSubtype
#: references form a loop.
_MAX_DEPTH = 32
#: How far up a method's object type hierarchy ownership is looked for.
_MAX_TYPE_DEPTH = 8

_NS0_NUMERIC = re.compile(r"ns=0;i=([0-9]+)")

#: The bit of AccessLevel / UserAccessLevel that allows writing the Value.
CURRENT_WRITE = 0x02
#: The bit that says the server keeps the Value's history (Part 3 §5.6.2).
HISTORY_READ = 0x04

#: The attributes round one reads, in the order they are requested, and the
#: field of the record each one fills.
_ATTRIBUTES: tuple[tuple[str, int], ...] = (
    ("node_class", ua.AttributeIds.NodeClass),
    ("data_type_id", ua.AttributeIds.DataType),
    ("value_rank", ua.AttributeIds.ValueRank),
    ("array_dimensions", ua.AttributeIds.ArrayDimensions),
    ("access_level", ua.AttributeIds.AccessLevel),
    ("user_access_level", ua.AttributeIds.UserAccessLevel),
    ("executable", ua.AttributeIds.Executable),
    ("user_executable", ua.AttributeIds.UserExecutable),
)

#: The properties round three resolves on a Variable, and on an enumerated
#: DataType node.
_VARIABLE_PROPERTIES = ("0:EnumStrings", "0:EnumValues", "0:TrueState", "0:FalseState")
_TYPE_PROPERTIES = ("0:EnumStrings", "0:EnumValues")


def resolve_data_type(
    data_type_id: str | None, supertype: Callable[[str], str | None]
) -> dict[str, Any]:
    """What a DataType is written as: ``{"data_type": name | None, "enumeration": bool}``.

    Walks ``data_type_id`` up its supertypes (``supertype`` gives the parent of
    one, or None) to the first node that answers the question. Enumeration is an
    Int32 on the wire; a concrete built-in is itself, if it is one of the fifteen
    spellings a write accepts; an abstract one — Structure, BaseDataType, Number —
    names no encoding, so ``None``, and the write converts to the current value's
    type instead.

    Never raises. A walk that loops, runs past ``_MAX_DEPTH``, dead-ends or whose
    lookup fails is a type that could not be resolved, and that is a check that is
    skipped rather than a write that is refused. Driven by
    ``tests/fixtures/data-type-resolution.json`` for both runtimes.
    """
    unresolved = {"data_type": None, "enumeration": False}
    current = canonical_node_id(data_type_id) if data_type_id else None
    seen: set[str] = set()
    try:
        for _ in range(_MAX_DEPTH):
            if current is None or current in seen:
                return unresolved
            seen.add(current)
            match = _NS0_NUMERIC.fullmatch(current)
            if match:
                identifier = int(match.group(1))
                if identifier == _ENUMERATION:
                    return {"data_type": "Int32", "enumeration": True}
                if 1 <= identifier <= _LAST_CONCRETE:
                    name = ua.VariantType(identifier).name
                    return {
                        "data_type": name if name in WRITE_DATA_TYPES else None,
                        "enumeration": False,
                    }
                if identifier <= _LAST_ABSTRACT:
                    return unresolved
            current = supertype(current)
    except Exception:
        return unresolved
    return unresolved


def allows(level: int | None, bit: int) -> bool | None:
    """Whether an AccessLevel byte has ``bit`` set; None when the level is unknown."""
    return None if level is None else bool(level & bit)


def empty_facts(status: str) -> dict[str, Any]:
    """The record for a node whose NodeClass could not be read: only its status."""
    return {
        "status": status,
        "node_class": None,
        "data_type": None,
        "data_type_id": None,
        "enumeration": False,
        "value_rank": None,
        "array_dimensions": None,
        "access_level": None,
        "user_access_level": None,
        "executable": None,
        "user_executable": None,
        "states": None,
        "two_state": None,
    }


def _good(data_value: Any) -> bool:
    status = getattr(data_value, "StatusCode", None)
    return status is None or status.is_good()


def _status_name(data_value: Any) -> str:
    status = getattr(data_value, "StatusCode", None)
    return "Good" if status is None else str(status.name)


def _value(data_value: Any) -> Any:
    return getattr(getattr(data_value, "Value", None), "Value", None)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _attribute(field: str, data_value: Any) -> Any:
    """One attribute's value as the record spells it, or None if it is not one."""
    if not _good(data_value):
        return None
    value = _value(data_value)
    if field == "node_class":
        try:
            return ua.NodeClass(value).name
        except (TypeError, ValueError):
            return None
    if field == "data_type_id":
        return node_id_text(value) if isinstance(value, ua.NodeId) else None
    if field == "array_dimensions":
        if not isinstance(value, (list, tuple)):
            return None
        dimensions = [_integer(item) for item in value]
        return None if not dimensions or None in dimensions else dimensions
    if field in {"executable", "user_executable"}:
        return value if isinstance(value, bool) else None
    return _integer(value)


def _text(value: Any) -> str | None:
    """The readable half of a LocalizedText, or None if it carries none."""
    text = getattr(value, "Text", None)
    return str(text) if text else None


def states_from(enum_strings: Any, enum_values: Any) -> list[dict[str, Any]] | None:
    """An enumeration's states, from whichever of its two properties says something.

    ``EnumStrings`` numbers its labels by position; ``EnumValues`` carries each
    value with its label, which is how an enumeration with gaps is published. A
    label that is empty names nothing a write could use and is left out; a list
    left empty is no states at all.
    """
    if isinstance(enum_strings, (list, tuple)):
        found = [
            {"value": index, "label": label}
            for index, item in enumerate(enum_strings)
            if (label := _text(item))
        ]
        if found:
            return found
    if isinstance(enum_values, (list, tuple)):
        found = []
        for item in enum_values:
            value = _integer(getattr(item, "Value", None))
            label = _text(getattr(item, "DisplayName", None))
            if value is not None and label:
                found.append({"value": value, "label": label})
        if found:
            return found
    return None


def two_state_from(true_state: Any, false_state: Any) -> dict[str, str] | None:
    """A two-state node's labels, only when it names both."""
    true_text, false_text = _text(true_state), _text(false_state)
    if true_text and false_text:
        return {"true": true_text, "false": false_text}
    return None


class NodeFacts:
    """The per-session cache of what each node's attributes said."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        #: DataType id -> what it resolved to. Per DataType rather than per node:
        #: a plant has thousands of Doubles and one Double.
        self._types: dict[str, dict[str, Any]] = {}
        #: Enumerated DataType id -> the states published on the DataType node.
        self._type_states: dict[str, list[dict[str, Any]] | None] = {}
        #: (object, method) -> whether the method was found on the object or its type.
        self._on_object: dict[tuple[str, str], bool] = {}
        #: Set when this server answered "no" to TranslateBrowsePaths itself, as
        #: NodeMetadata remembers it: asking again every write would cost a round
        #: trip and a line of stderr each time.
        self._unanswerable = False
        #: What the connected server says one request may carry. Set by the
        #: capability probe on every session, like NodeMetadata's.
        self.server_limits: dict[str, int | None] = dict(UNSTATED)

    def forget(self) -> None:
        """Drop everything, because the session it was true of is gone."""
        self._cache.clear()
        self._types.clear()
        self._type_states.clear()
        self._on_object.clear()
        self._unanswerable = False

    def cached(self, node_id: str) -> dict[str, Any] | None:
        """What is already known about ``node_id``, without asking the server."""
        return self._cache.get(node_id)

    def for_nodes(self, client: Any, node_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Each node's facts, reading only the ones not already known. Never raises."""
        wanted = list(dict.fromkeys(node_ids))
        missing = [node_id for node_id in wanted if node_id not in self._cache]
        found: dict[str, dict[str, Any] | None] = {}
        if missing:
            try:
                found = self._read(client, missing)
            except Exception as error:
                print(f"Could not read node attributes: {describe_error(error)}", file=sys.stderr)
        return {node_id: self._cache.get(node_id) or found.get(node_id) for node_id in wanted}

    # --- round one ---------------------------------------------------------------

    def _read(self, client: Any, node_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """The three rounds for ``node_ids``; caches whatever was read in full."""
        parsed: dict[str, Any] = {}
        for node_id in node_ids:
            try:
                parsed[node_id] = client.get_node(node_id).nodeid
            except Exception:
                # Not a node id at all. Whatever the tool does with it next will
                # say so better than a facts read could.
                continue
        reads = []
        for nodeid in parsed.values():
            for _, attribute in _ATTRIBUTES:
                read = ua.ReadValueId()
                read.NodeId = nodeid
                read.AttributeId = attribute
                reads.append(read)
        values: list[Any] = []
        for part in chunked(reads, read_chunk(self.server_limits)):
            params = ua.ReadParameters()
            params.NodesToRead = part
            values.extend(client.uaclient.read(params))

        width = len(_ATTRIBUTES)
        records: dict[str, dict[str, Any]] = {}
        for index, node_id in enumerate(parsed):
            answers = values[index * width : (index + 1) * width]
            if not _good(answers[0]):
                records[node_id] = empty_facts(_status_name(answers[0]))
                continue
            record = empty_facts("Good")
            for (field, _), data_value in zip(_ATTRIBUTES, answers, strict=True):
                record[field] = _attribute(field, data_value)
            records[node_id] = record

        # What could not be learned in full is returned and not remembered: a
        # connection error is not an answer about the address space.
        incomplete: set[str] = set()
        incomplete |= self._resolve_types(client, records)
        incomplete |= self._read_states(client, records)
        for node_id, record in records.items():
            if node_id not in incomplete:
                self._cache[node_id] = record
        return dict(records)

    # --- round two ---------------------------------------------------------------

    def _resolve_types(self, client: Any, records: dict[str, dict[str, Any]]) -> set[str]:
        """Fill ``data_type`` and ``enumeration``; the nodes whose type could not be walked."""
        failed: set[str] = set()
        for data_type_id in {r["data_type_id"] for r in records.values() if r["data_type_id"]}:
            if data_type_id in self._types:
                continue
            errors: list[BaseException] = []

            def parent(node_id: str, errors: list[BaseException] = errors) -> str | None:
                try:
                    return supertype_of(client, node_id)
                except Exception as error:
                    errors.append(error)
                    raise

            resolved = resolve_data_type(data_type_id, parent)
            if errors:
                print(
                    f"Could not resolve DataType {data_type_id}: {describe_error(errors[0])}",
                    file=sys.stderr,
                )
                # The records keep their unresolved type; nothing is cached.
                failed.add(data_type_id)
                continue
            self._types[data_type_id] = resolved

        incomplete = set()
        for node_id, record in records.items():
            if record["data_type_id"] in failed:
                incomplete.add(node_id)
            elif record["data_type_id"] in self._types:
                record.update(self._types[record["data_type_id"]])
        return incomplete

    # --- round three -------------------------------------------------------------

    def _read_states(self, client: Any, records: dict[str, dict[str, Any]]) -> set[str]:
        """Fill ``states`` and ``two_state``; the nodes whose properties could not be read."""
        variables = [
            node_id
            for node_id, record in records.items()
            if record["node_class"] == "Variable"
            and (record["data_type"] in STATE_DATA_TYPES or record["enumeration"])
        ]
        types = sorted(
            {
                records[node_id]["data_type_id"]
                for node_id in variables
                if records[node_id]["enumeration"]
                and records[node_id]["data_type_id"] not in self._type_states
            }
        )
        if not variables:
            return set()
        if self._unanswerable:
            # Asked and refused already this session; the states stay unknown,
            # and that is an answer worth keeping.
            self._apply_states(records, variables, {}, self._type_states)
            return set()

        owners: list[tuple[str, str, str]] = []
        paths = []
        for node_id in variables:
            for name in _VARIABLE_PROPERTIES:
                owners.append(("variable", node_id, name))
                paths.append(property_path(client.get_node(node_id).nodeid, name))
        for data_type_id in types:
            for name in _TYPE_PROPERTIES:
                owners.append(("type", data_type_id, name))
                paths.append(property_path(client.get_node(data_type_id).nodeid, name))

        try:
            results = [
                result
                for chunk in chunked(paths, translate_chunk(self.server_limits, MAX_PER_REQUEST))
                for result in client.uaclient.translate_browsepaths_to_nodeids(chunk)
            ]
        except Exception as error:
            self._unanswerable = not is_connection_error(error)
            print(
                f"Could not read enumeration states: {describe_error(error)}"
                + ("; not asking this session again" if self._unanswerable else ""),
                file=sys.stderr,
            )
            self._apply_states(records, variables, {}, self._type_states)
            return set() if self._unanswerable else set(variables)

        targets = []
        targeted: list[tuple[str, str, str]] = []
        for owner, result in zip(owners, results, strict=True):
            target = first_target(result)
            if target is not None:
                targets.append(target)
                targeted.append(owner)
        try:
            values = [
                value
                for chunk in chunked(targets, read_chunk(self.server_limits, MAX_PER_REQUEST))
                for value in client.uaclient.get_attributes(chunk, ua.AttributeIds.Value)
            ]
        except Exception as error:
            print(f"Could not read enumeration states: {describe_error(error)}", file=sys.stderr)
            self._apply_states(records, variables, {}, self._type_states)
            return set(variables)

        on_variables: dict[str, dict[str, Any]] = {}
        on_types: dict[str, dict[str, Any]] = {data_type_id: {} for data_type_id in types}
        for (kind, owner, name), data_value in zip(targeted, values, strict=True):
            if not _good(data_value):
                continue
            bucket = on_variables if kind == "variable" else on_types
            bucket.setdefault(owner, {})[name] = _value(data_value)
        for data_type_id, found in on_types.items():
            self._type_states[data_type_id] = states_from(
                found.get("0:EnumStrings"), found.get("0:EnumValues")
            )
        self._apply_states(records, variables, on_variables, self._type_states)
        return set()

    @staticmethod
    def _apply_states(
        records: dict[str, dict[str, Any]],
        variables: list[str],
        on_variables: dict[str, dict[str, Any]],
        on_types: dict[str, list[dict[str, Any]] | None],
    ) -> None:
        """A variable's own states first, then its enumerated DataType's."""
        for node_id in variables:
            record = records[node_id]
            found = on_variables.get(node_id, {})
            states = states_from(found.get("0:EnumStrings"), found.get("0:EnumValues"))
            if states is None and record["enumeration"]:
                states = on_types.get(record["data_type_id"])
            record["states"] = states
            record["two_state"] = two_state_from(
                found.get("0:TrueState"), found.get("0:FalseState")
            )

    # --- method ownership --------------------------------------------------------

    def on_object(self, client: Any, object_node_id: str, method_node_id: str) -> bool | None:
        """Whether a method is one of the object's, or of its type's; None if unknown.

        Part 4 §5.11.2: a server runs a method on an object only if the method is
        a component of that object or of its ObjectType. The object's forward
        references first; then its type definition and that type's supertypes,
        each one's forward references, ``_MAX_TYPE_DEPTH`` deep. Every browse
        answering and the method found nowhere is ``False``; any browse failing is
        ``None``, and ``None`` skips the check. Cached per session per pair.
        """
        key = (canonical_node_id(object_node_id), canonical_node_id(method_node_id))
        if key in self._on_object:
            return self._on_object[key]
        try:
            answer = _method_on_object(client, *key)
        except Exception as error:
            print(
                f"Could not tell whether {key[1]} is a method of {key[0]}: {describe_error(error)}",
                file=sys.stderr,
            )
            return None
        self._on_object[key] = answer
        return answer


def _method_on_object(client: Any, object_node_id: str, method_node_id: str) -> bool:
    references = browse_references(
        client,
        object_node_id,
        direction=ua.BrowseDirection.Both,
        reference_type=ua.ObjectIds.References,
    )
    forward = [reference for reference in references if reference.IsForward]
    if any(node_id_text(reference.NodeId) == method_node_id for reference in forward):
        return True
    type_definition = ua.NodeId(ua.ObjectIds.HasTypeDefinition)
    current = next(
        (
            node_id_text(reference.NodeId)
            for reference in forward
            if reference.ReferenceTypeId == type_definition
        ),
        None,
    )
    seen: set[str] = set()
    has_subtype = ua.NodeId(ua.ObjectIds.HasSubtype)
    for _ in range(_MAX_TYPE_DEPTH):
        if current is None or current in seen:
            break
        seen.add(current)
        references = browse_references(
            client,
            current,
            direction=ua.BrowseDirection.Both,
            reference_type=ua.ObjectIds.References,
        )
        if any(
            reference.IsForward and node_id_text(reference.NodeId) == method_node_id
            for reference in references
        ):
            return True
        current = next(
            (
                node_id_text(reference.NodeId)
                for reference in references
                if not reference.IsForward and reference.ReferenceTypeId == has_subtype
            ),
            None,
        )
    return False


__all__ = [
    "CURRENT_WRITE",
    "HISTORY_READ",
    "STATE_DATA_TYPES",
    "WRITE_DATA_TYPES",
    "NodeFacts",
    "allows",
    "empty_facts",
    "resolve_data_type",
    "states_from",
    "two_state_from",
]
