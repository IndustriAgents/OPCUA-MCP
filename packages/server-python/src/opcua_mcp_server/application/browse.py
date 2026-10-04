"""Address-space traversal over normalized references; no MCP or native SDK."""

from __future__ import annotations

from collections import deque
from typing import Protocol

from ..completeness import traversal_completeness
from ..contract import CONTRACT
from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..node_ids import canonical_node_id


class BrowsePort(Protocol):
    async def children(self, node_id: str) -> list[dict]: ...
    async def describe(self, node_id: str, parent_node_id: str) -> dict: ...
    async def enrich(self, records: list[dict], include_values: bool) -> None: ...


def browse_name_matches(segment: str, namespace_index: int, name: str) -> bool:
    prefix, separator, rest = segment.partition(":")
    if separator and prefix.isdigit():
        return int(prefix) == namespace_index and rest == name
    return segment == name


async def resolve_path(port: BrowsePort, start: str, path: str) -> str:
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        raise ApplicationRefusal(f'browse_path "{path}" names no elements')
    current = "ns=0;i=84" if path.startswith("/") else canonical_node_id(start)
    for segment in segments:
        refs = await port.children(current)
        match = next(
            (
                ref
                for ref in refs
                if browse_name_matches(segment, ref["namespaceIndex"], ref["name"])
            ),
            None,
        )
        if match is None:
            raise ApplicationRefusal(
                f'browse_path "{path}" does not resolve: no child "{segment}" under {current}'
            )
        current = match["nodeId"]
    return current


async def browse_nodes(
    port: BrowsePort,
    *,
    node_id: str,
    browse_path: str | None,
    depth: int,
    node_class: str | None,
    name_filter: str | None,
    include_values: bool,
    max_nodes: int,
) -> dict:
    limits = CONTRACT["traversal"]
    depth = max(0, min(limits["maxDepth"], int(depth)))
    max_nodes = max(1, min(limits["maxNodes"], int(max_nodes)))
    wanted_class = node_class.lower() if node_class else None
    wanted_name = name_filter.lower() if name_filter else None
    root = (
        await resolve_path(port, node_id, browse_path)
        if browse_path
        else canonical_node_id(node_id)
    )

    def keep(record):
        return (wanted_class is None or record["node_class"].lower() == wanted_class) and (
            wanted_name is None or wanted_name in record["browse_name"].lower()
        )

    try:
        found = []
        inspected = 0
        truncated = unbrowsable = False
        if depth == 0:
            inspected = 1
            record = await port.describe(root, root)
            if keep(record):
                found.append(record)
        else:
            queue = deque([(root, 0)])
            visited = {root}
            while queue and not truncated:
                current_id, current_depth = queue.popleft()
                try:
                    refs = await port.children(current_id)
                except Exception:
                    if current_id == root:
                        raise
                    unbrowsable = True
                    continue
                for ref in refs:
                    child_id = ref["nodeId"]
                    if child_id in visited:
                        continue
                    visited.add(child_id)
                    if inspected >= max_nodes:
                        truncated = True
                        break
                    inspected += 1
                    if ref["name"] == limits["skipBrowseName"]:
                        continue
                    record = {
                        "node_id": child_id,
                        "browse_name": f"{ref['namespaceIndex']}:{ref['name']}",
                        "node_class": ref["nodeClass"],
                        "parent_node_id": current_id,
                        "data_type": None,
                        "value": None,
                        "description": None,
                        "type_definition": None,
                    }
                    if keep(record):
                        found.append(record)
                    if ref["nodeClass"] == "Object" and current_depth + 1 < depth:
                        queue.append((child_id, current_depth + 1))
        await port.enrich(found, include_values)
        return {
            "result": {"nodes": found, "truncated": truncated, "inspected": inspected},
            "completeness": traversal_completeness(
                returned=len(found),
                truncated=truncated,
                max_nodes=max_nodes,
                unbrowsable=unbrowsable,
            ),
        }
    except Exception as error:
        raise AdapterFailure(
            "browse", message("browseFailed", node_id=root, reason=describe_error(error)), error
        ) from error
