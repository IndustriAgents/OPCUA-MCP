"""What a node says its number means: the unit, and the ranges around it.

``node-metadata.ts`` is the Node half and must produce the same records from the
same address space, because ``resultShapes.nodeValues`` -> ``engineering`` is one
shape and a client that learns one server's output has to be able to read the
other's.

A bare ``51.75`` cannot be told from °C, PSI or %, and cannot be told from a
trip. OPC UA Part 8 §5.3 introduces ``EngineeringUnits`` by citing the Mars
Climate Orbiter, and defines ``EURange`` as the range a value is expected to
occupy in normal operation and ``InstrumentRange`` as what the instrument can
physically return. All three are standard properties of ``AnalogItemType``, which
is what a real PLC or SCADA server exposes an analogue tag as — so this is
information the plant has already published and nobody was reading.

Two round trips for a whole batch, then nothing
-----------------------------------------------

Resolving three properties per node by browsing would be three round trips per
node, which would make a 500-node read unusable. Instead every property of every
uncached node goes into one ``TranslateBrowsePathsToNodeIds``, and everything
that resolved goes into one ``Read``. So a cold call costs two extra round trips
whatever the batch size, and a warm one costs none. (Two *logical* round trips:
both are chunked, because 500 nodes is 1500 browse paths and
``MaxNodesPerTranslateBrowsePathsToNodeIds`` is an operational limit a conformant
server may enforce.)

The cache is per session and is dropped when the session is replaced
(:meth:`forget`): a node's unit does not change while a session lasts, and a
server that has been restarted may not be the same server.

A failure is not cached, with one exception. "This node has no EURange" is an
answer worth remembering; "the connection died while we asked" is not, and
remembering it would leave a whole session's readings unqualified because of one
bad moment. But a server that cannot answer the *question* — one that does not
implement TranslateBrowsePathsToNodeIds — will not learn to, and asking it again
on every read would cost a round trip and a line of stderr each time. So that
answer is remembered, for the session.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from opcua import ua

from .connection import is_connection_error
from .contract import CONTRACT

_ANALOG = CONTRACT["analog"]

#: How many browse paths or node reads one request may carry. A 500-node read is
#: 1500 browse paths, and ``MaxNodesPerTranslateBrowsePathsToNodeIds`` is an
#: operational limit servers publish and enforce — so the batch is chunked rather
#: than sent as one request a conformant server is entitled to refuse.
MAX_PER_REQUEST: int = _ANALOG["maxPropertiesPerRequest"]

#: The three properties, in the order their browse paths are built and read.
PROPERTY_BROWSE_NAMES: tuple[str, str, str] = (
    _ANALOG["engineeringUnitsBrowseName"],
    _ANALOG["euRangeBrowseName"],
    _ANALOG["instrumentRangeBrowseName"],
)


@dataclass(frozen=True)
class Range:
    """One ``Range`` structure: what the two numbers in it are called here."""

    low: float
    high: float

    def to_json(self) -> dict[str, float]:
        return {"low": self.low, "high": self.high}

    def contains(self, value: float) -> bool:
        """Inclusive on both ends, as OPC UA's Range is."""
        return self.low <= value <= self.high


@dataclass(frozen=True)
class AnalogInfo:
    """What one node published about its own number."""

    unit: str | None = None
    unit_description: str | None = None
    eu_range: Range | None = None
    instrument_range: Range | None = None

    @property
    def is_empty(self) -> bool:
        """True when the node published nothing, which is most nodes."""
        return not any((self.unit, self.unit_description, self.eu_range, self.instrument_range))

    def to_json(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "unit_description": self.unit_description,
            "eu_range": self.eu_range.to_json() if self.eu_range else None,
            "instrument_range": (
                self.instrument_range.to_json() if self.instrument_range else None
            ),
        }


def _localized_text(value: Any) -> str | None:
    """The readable half of a ``LocalizedText``, or None if it carries none."""
    text = getattr(value, "Text", None)
    return str(text) if text else None


def _engineering_units(value: Any) -> tuple[str | None, str | None]:
    """The display name and description out of an ``EUInformation``."""
    if value is None:
        return None, None
    return _localized_text(getattr(value, "DisplayName", None)), _localized_text(
        getattr(value, "Description", None)
    )


def _range(value: Any) -> Range | None:
    """A ``Range`` structure as this module's record, or None if it is not one."""
    low = getattr(value, "Low", None)
    high = getattr(value, "High", None)
    if low is None or high is None:
        return None
    try:
        return Range(float(low), float(high))
    except (TypeError, ValueError):
        return None


def _browse_path(node_id: Any, browse_name: str) -> ua.BrowsePath:
    """One ``<index>:<name>`` hop from ``node_id``, as a BrowsePath.

    ``HierarchicalReferences`` rather than ``HasProperty`` (and subtypes
    included), because a server is free to expose these through any hierarchical
    reference and naming the narrower one would make this find nothing on a
    server that is still conformant.
    """
    index, _, name = browse_name.partition(":")
    element = ua.RelativePathElement()
    element.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    element.IsInverse = False
    element.IncludeSubtypes = True
    element.TargetName = ua.QualifiedName(name, int(index))
    path = ua.BrowsePath()
    path.StartingNode = node_id
    path.RelativePath = ua.RelativePath()
    path.RelativePath.Elements = [element]
    return path


class NodeMetadata:
    """The per-session cache of what each node said about its own number."""

    def __init__(self) -> None:
        self._cache: dict[str, AnalogInfo | None] = {}
        #: Set when this server answered "no" to the question itself rather than
        #: to one node — a server that does not implement
        #: TranslateBrowsePathsToNodeIds, say. Without it every read would pay a
        #: failed round trip and write a line to stderr, forever.
        self._unanswerable = False

    def forget(self) -> None:
        """Drop everything, because the session it was true of is gone."""
        self._cache.clear()
        self._unanswerable = False

    def cached(self, node_id: str) -> AnalogInfo | None:
        """What is already known about ``node_id``, without asking the server."""
        return self._cache.get(node_id)

    def for_nodes(self, client: Any, node_ids: list[str]) -> dict[str, AnalogInfo | None]:
        """What each node publishes, reading only the ones not already known.

        Best-effort throughout: a node that publishes nothing, a server that does
        not implement TranslateBrowsePaths, and a read that comes back Bad all
        produce ``None`` for that node rather than an error. A reading without
        its unit is worth less than one with it and is still worth returning —
        this must never be the reason a read fails.
        """
        wanted = list(dict.fromkeys(node_ids))
        missing = [node_id for node_id in wanted if node_id not in self._cache]
        if missing and not self._unanswerable:
            try:
                self._cache.update(self._read(client, missing))
            except Exception as error:
                # A connection error is not an answer about the address space,
                # and remembering it as one would leave a whole session's
                # readings unqualified because of one bad moment. Anything else
                # *is* an answer — a server that cannot translate browse paths
                # will not learn to — and asking it again every read would cost a
                # round trip and a line of stderr each time.
                self._unanswerable = not is_connection_error(error)
                print(
                    f"Could not read engineering units and ranges: {error}"
                    + ("; not asking this session again" if self._unanswerable else ""),
                    file=sys.stderr,
                )
        return {node_id: self._cache.get(node_id) for node_id in wanted}

    def _read(self, client: Any, node_ids: list[str]) -> dict[str, AnalogInfo | None]:
        """One translate and one read for the whole batch. See the module docstring."""
        paths = [
            _browse_path(client.get_node(node_id).nodeid, browse_name)
            for node_id in node_ids
            for browse_name in PROPERTY_BROWSE_NAMES
        ]
        results = [
            result
            for chunk in _chunked(paths)
            for result in client.uaclient.translate_browsepaths_to_nodeids(chunk)
        ]

        # Which property of which node each resolved target belongs to, so the
        # one flat read below can be put back into records.
        targets: list[Any] = []
        owners: list[tuple[int, int]] = []
        for index, result in enumerate(results):
            target = _first_target(result)
            if target is None:
                continue
            owners.append((index // len(PROPERTY_BROWSE_NAMES), index % len(PROPERTY_BROWSE_NAMES)))
            targets.append(target)

        values: list[Any] = [
            value
            for chunk in _chunked(targets)
            for value in client.uaclient.get_attributes(chunk, ua.AttributeIds.Value)
        ]

        found: dict[str, list[Any]] = {
            node_id: [None] * len(PROPERTY_BROWSE_NAMES) for node_id in node_ids
        }
        for (node_index, property_index), data_value in zip(owners, values, strict=True):
            status = getattr(data_value, "StatusCode", None)
            if status is not None and not status.is_good():
                continue
            found[node_ids[node_index]][property_index] = getattr(
                getattr(data_value, "Value", None), "Value", None
            )

        return {node_id: _assemble(properties) for node_id, properties in found.items()}


def _chunked(items: list[Any]) -> list[list[Any]]:
    """``items`` in requests no larger than the server is obliged to accept."""
    return [
        items[start : start + MAX_PER_REQUEST] for start in range(0, len(items), MAX_PER_REQUEST)
    ]


def _first_target(result: Any) -> Any:
    """The NodeId a browse path resolved to, or None if it resolved to nothing.

    A node with no EURange answers ``BadNoMatch``, which is an answer and not a
    failure: most nodes are not AnalogItems.
    """
    status = getattr(result, "StatusCode", None)
    if status is not None and not status.is_good():
        return None
    targets = getattr(result, "Targets", None) or []
    return targets[0].TargetId if targets else None


def _assemble(properties: list[Any]) -> AnalogInfo | None:
    """One node's three property values as a record, or None if it published none."""
    units, eu_range, instrument_range = properties
    unit, unit_description = _engineering_units(units)
    info = AnalogInfo(
        unit=unit,
        unit_description=unit_description,
        eu_range=_range(eu_range),
        instrument_range=_range(instrument_range),
    )
    return None if info.is_empty else info


__all__ = ["PROPERTY_BROWSE_NAMES", "AnalogInfo", "NodeMetadata", "Range"]
