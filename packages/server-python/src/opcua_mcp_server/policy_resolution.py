"""The policy, resolved against the server this session is connected to.

A policy file names things the server has to say what they are: a browse path
is a name until a server answers which node it reaches, a ``writable_subtrees``
rule is a set of nodes only once someone walks the subtree, and a ``deny_read``
entry hides nothing beneath it until its subtree has been listed. This module
asks, once per session — after the NamespaceArray is bound, because an ``nsu=``
entry needs it — binds the answers into the policy, and then measures the policy
against the server (``policy_check.policy_findings``) so that an entry that
allows nothing the server will accept is reported now rather than discovered by
the one write that mattered.

Best-effort throughout, and it never fails the connect. But the two halves fail
in opposite directions, on purpose. An allowlist entry that cannot be resolved
allows nothing: the plant model only ever narrows. A ``deny_read`` that cannot be
resolved in full makes the read policy *incomplete*, and every guarded read is
then refused until a session resolves it: hiding part of what the operator said
to hide would be a confidentiality rule that silently does not hold.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opcua import ua

from .address_space import (
    ROOT_FOLDER,
    browse_many,
    browse_name_matches,
    browse_references,
    node_id_text,
)
from .connection import describe_error
from .contract import CONTRACT
from .node_facts import NodeFacts
from .node_metadata import NodeMetadata
from .policy import MAX_DENY_NODES, ToolPolicy, ValueBound
from .policy_check import finding_message, policy_findings

_MAX_DEPTH: int = CONTRACT["traversal"]["maxDepth"]


@dataclass(frozen=True)
class PathResolution:
    """What one browse path named: a node, or why it named none."""

    node_id: str | None
    #: ``unresolved`` or ``pathAmbiguous`` when ``node_id`` is None.
    reason: str | None = None
    #: For an ambiguous path, the segment that matched twice and where.
    segment: str | None = None
    parent: str | None = None


def resolve_path(client: Any, path: str) -> PathResolution:
    """Resolve a policy browse path from the Root folder, refusing an ambiguous one.

    Segment by segment, like ``browse_opcua_nodes``' ``browse_path`` — ``2:Name``
    in namespace 2, a bare ``Name`` in any namespace — with one difference that
    matters for a policy: a segment matching more than one child resolves to
    nothing. A browse picks the first match because it is showing someone where
    to look; an allowlist that picked the first of two ``Pump`` nodes would
    authorize whichever one the server happened to list first. Raises when a
    browse fails, which is not the same answer as "no such node".
    """
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return PathResolution(None, "unresolved")
    current = ROOT_FOLDER
    for segment in segments:
        matches = list(
            dict.fromkeys(
                node_id_text(reference.NodeId)
                for reference in browse_references(client, current)
                if browse_name_matches(
                    segment, reference.BrowseName.NamespaceIndex, reference.BrowseName.Name
                )
            )
        )
        if not matches:
            return PathResolution(None, "unresolved", segment, current)
        if len(matches) > 1:
            return PathResolution(None, "pathAmbiguous", segment, current)
        current = matches[0]
    return PathResolution(current)


class WalkFailed(Exception):
    """A subtree walk that could not be completed, with the status or error that stopped it."""


def walk(
    client: Any,
    root: str,
    server_limits: dict[str, int | None],
    *,
    keep: Callable[[Any], bool],
    limit: int,
    strict: bool,
    type_definition: str | None = None,
) -> tuple[list[str], bool] | None:
    """Every node under ``root`` that ``keep`` accepts, and whether there were more than ``limit``.

    Forward hierarchical references, one batched Browse per level, at most
    ``traversal.maxDepth`` levels down, skipping nothing but nodes already seen —
    not even the Server object a browse skips, because a policy covers what it
    says it covers. ``root`` itself is never a candidate. Stops as soon as
    ``limit`` is passed: the answer is then "too many", and listing the rest
    would only cost the server.

    With ``type_definition``, a node ``keep`` accepts must also have exactly that
    HasTypeDefinition — asked of the node itself, in one more batched Browse per
    level, rather than taken from the parent's ReferenceDescription.TypeDefinition.
    That field is the parent's copy, and python-opcua's server fills it in when
    the child is created and never again, so a node retyped afterwards is listed
    under its old type; ``browse_opcua_nodes`` reports ``type_definition`` from
    the same HasTypeDefinition browse, and a rule must match what a browse shows.

    None when the root itself is unknown to the server (BadNodeIdUnknown), which
    is "names no node" rather than a failure. A Bad status below the root raises
    :class:`WalkFailed` when ``strict`` (a deny walk must be whole), and is
    skipped otherwise.
    """
    matched: list[str] = []
    visited = {root}
    level = [root]
    for depth in range(_MAX_DEPTH):
        if not level:
            break
        following: list[str] = []
        candidates: list[str] = []
        for status, references in browse_many(client, level, server_limits):
            if not status.is_good():
                if depth == 0:
                    if status.value == ua.StatusCodes.BadNodeIdUnknown:
                        return None
                    raise WalkFailed(str(status.name))
                if strict:
                    raise WalkFailed(str(status.name))
                continue
            for reference in references:
                child = node_id_text(reference.NodeId)
                if child in visited:
                    continue
                visited.add(child)
                following.append(child)
                if keep(reference):
                    candidates.append(child)
        if type_definition is not None and candidates:
            candidates = [
                child
                for child, (status, references) in zip(
                    candidates,
                    browse_many(
                        client,
                        candidates,
                        server_limits,
                        reference_type=ua.ObjectIds.HasTypeDefinition,
                        # HasTypeDefinition has no subtypes; asking for them
                        # would let an invented one through.
                        include_subtypes=False,
                    ),
                    strict=True,
                )
                if status.is_good()
                and len(references) == 1
                and node_id_text(references[0].NodeId) == type_definition
            ]
        matched.extend(candidates)
        if len(matched) > limit:
            return matched, True
        level = following
    return matched, False


@dataclass
class Resolution:
    """What one session's resolution found, before it is measured."""

    paths: dict[str, PathResolution] = field(default_factory=dict)
    #: Why a path could not be resolved at all — a browse that failed — by path.
    path_errors: dict[str, str] = field(default_factory=dict)
    subtrees: list[dict[str, Any]] = field(default_factory=list)
    subtree_bounds: dict[str, ValueBound] = field(default_factory=dict)
    deny: list[dict[str, Any]] = field(default_factory=list)
    deny_nodes: set[str] = field(default_factory=set)
    deny_complete: bool = True
    deny_reason: str | None = None


def _path_entries(policy: ToolPolicy, operator: bool) -> list[str]:
    """Every browse path the policy uses, in policy order, once each.

    deny_read under every profile; the control rules only under operator, which
    is the only profile that applies them.
    """
    config = policy.config
    entries: list[str] = []
    if operator:
        entries.extend(config.value_bounds)
        for entry in config.callable_method_entries:
            entries.extend(entry.partition("|")[::2])
        entries.extend(rule.root for rule in config.writable_subtrees)
    entries.extend(config.deny_read)
    if operator:
        entries.extend(config.alarm_sources)
        for rule in config.preconditions:
            entries.extend(rule.targets)
            for object_id, method_id in rule.methods:
                entries.extend((object_id, method_id))
            entries.extend(requirement["node"] for requirement in rule.require)
    return [entry for entry in dict.fromkeys(entries) if entry.startswith("/")]


def _resolve_paths(client: Any, paths: list[str], resolution: Resolution) -> None:
    for path in paths:
        try:
            resolution.paths[path] = resolve_path(client, path)
        except Exception as error:
            reason = describe_error(error)
            print(f"Could not resolve policy browse path {path}: {reason}", file=sys.stderr)
            resolution.paths[path] = PathResolution(None, "unresolved")
            resolution.path_errors[path] = reason


def _expand_subtrees(
    client: Any, policy: ToolPolicy, server_limits: dict, resolution: Resolution
) -> None:
    """Each writable_subtrees rule's matches, or why it contributes nothing."""
    for rule in policy.config.writable_subtrees:
        item = {"entry": rule.root, "outcome": "unresolved", "limit": rule.max_nodes}
        resolution.subtrees.append(item)
        root = policy.resolve(rule.root)
        if root is None:
            continue
        wanted_type = None
        if rule.type_definition is not None:
            wanted_type = policy.resolve(rule.type_definition)
            if wanted_type is None:
                # A type the server cannot be asked about matches no Variable.
                item["outcome"] = "empty"
                continue

        def keep(reference: Any) -> bool:
            return reference.NodeClass == ua.NodeClass.Variable

        try:
            walked = walk(
                client,
                root,
                server_limits,
                keep=keep,
                limit=rule.max_nodes,
                strict=False,
                type_definition=wanted_type,
            )
        except Exception as error:
            print(
                f"Could not expand writable_subtrees rule {rule.root}: {describe_error(error)}",
                file=sys.stderr,
            )
            continue
        if walked is None:
            continue
        matched, too_many = walked
        if too_many:
            item["outcome"] = "tooLarge"
        elif not matched:
            item["outcome"] = "empty"
        else:
            item["outcome"] = "ok"
            for node_id in matched:
                resolution.subtree_bounds.setdefault(node_id, rule.bound)


def _expand_deny(
    client: Any, policy: ToolPolicy, server_limits: dict, resolution: Resolution
) -> None:
    """The deny set: every entry and everything under it, capped in total."""
    for entry in policy.config.deny_read:
        item = {"entry": entry, "outcome": "ok", "limit": MAX_DENY_NODES, "reason": None}
        resolution.deny.append(item)
        if entry in resolution.path_errors:
            item.update(outcome="failed", reason=resolution.path_errors[entry])
            resolution.deny_complete = False
            continue
        root = policy.resolve(entry)
        if root is None:
            item["outcome"] = "unresolved"
            continue
        room = MAX_DENY_NODES - len(resolution.deny_nodes | {root})
        try:
            walked = walk(
                client,
                root,
                server_limits,
                keep=lambda reference: node_id_text(reference.NodeId) not in resolution.deny_nodes,
                limit=max(room, 0),
                strict=True,
            )
        except Exception as error:
            item.update(outcome="failed", reason=describe_error(error))
            resolution.deny_complete = False
            continue
        if walked is None:
            item["outcome"] = "unresolved"
            continue
        matched, too_many = walked
        resolution.deny_nodes.add(root)
        resolution.deny_nodes.update(matched)
        if too_many or len(resolution.deny_nodes) > MAX_DENY_NODES:
            item["outcome"] = "tooLarge"
            resolution.deny_complete = False
            # Nothing past the cap is listed: the set is incomplete whatever the
            # rest would add, and every read is refused until it is narrowed.
            break
    for item in resolution.deny:
        if item["outcome"] in {"tooLarge", "failed"}:
            resolution.deny_reason = _as_reason(
                finding_message(
                    "denyTooLarge" if item["outcome"] == "tooLarge" else "denyFailed",
                    entry=item["entry"],
                    limit=item["limit"],
                    reason=item["reason"],
                )
            )
            break


def _as_reason(problem: str) -> str:
    """A finding as readPolicyIncomplete's {reason}: without its full stop.

    The template carries on after the placeholder ("{reason}. get_server_status
    …"), so a sentence that kept its own would end in two.
    """
    return problem[:-1] if problem.endswith(".") else problem


def _bound_json(bound: ValueBound | None) -> dict[str, Any] | None:
    if bound is None or bound.is_empty:
        return None
    return {
        "min": bound.minimum,
        "max": bound.maximum,
        "enum": list(bound.allowed) if bound.allowed is not None else None,
        "max_change": bound.max_change,
    }


def _unresolved_reason(resolution: Resolution, entry: str) -> dict[str, Any]:
    """Why an entry resolved to nothing, as policy_findings' input spells it."""
    found = resolution.paths.get(entry)
    if found is not None and found.reason == "pathAmbiguous":
        return {
            "unresolved_reason": "pathAmbiguous",
            "segment": found.segment,
            "parent": found.parent,
        }
    return {"unresolved_reason": "unresolved", "segment": None, "parent": None}


def _lint_input(
    client: Any,
    policy: ToolPolicy,
    facts: NodeFacts,
    metadata: NodeMetadata,
    resolution: Resolution,
) -> dict[str, Any]:
    """policy_findings' input for the control rules: each entry, resolved, with its facts."""
    config = policy.config
    writable = [(entry, policy.resolve(entry)) for entry in config.value_bounds]
    methods = []
    for entry in config.callable_method_entries:
        object_id, _, method_id = entry.partition("|")
        methods.append(
            (entry, object_id, method_id, policy.resolve(object_id), policy.resolve(method_id))
        )

    nodes = [node_id for _, node_id in writable if node_id is not None]
    for _, _, _, object_node_id, method_node_id in methods:
        nodes.extend(node_id for node_id in (object_node_id, method_node_id) if node_id is not None)
    known = facts.for_nodes(client, nodes) if nodes else {}
    engineering = metadata.for_nodes(client, [n for _, n in writable if n is not None])

    check: dict[str, Any] = {"writable": [], "methods": []}
    for entry, node_id in writable:
        item = {
            "entry": entry,
            "node_id": node_id,
            "unresolved_reason": None,
            "segment": None,
            "parent": None,
            "bound": _bound_json(config.value_bounds[entry]),
            "facts": known.get(node_id) if node_id else None,
            "engineering": None,
        }
        if node_id is None:
            item.update(_unresolved_reason(resolution, entry))
        elif (info := engineering.get(node_id)) is not None:
            item["engineering"] = info.to_json()
        check["writable"].append(item)
    for entry, object_id, method_id, object_node_id, method_node_id in methods:
        item = {
            "entry": entry,
            "object_node_id": object_node_id,
            "method_node_id": method_node_id,
            "unresolved_reason": None,
            "segment": None,
            "parent": None,
            "object_facts": known.get(object_node_id) if object_node_id else None,
            "method_facts": known.get(method_node_id) if method_node_id else None,
            "on_object": None,
        }
        if object_node_id is None or method_node_id is None:
            item.update(
                _unresolved_reason(resolution, object_id if object_node_id is None else method_id)
            )
        else:
            item["on_object"] = facts.on_object(client, object_node_id, method_node_id)
        check["methods"].append(item)
    check["subtrees"] = resolution.subtrees
    check["alarm_sources"] = [
        {"entry": entry, "node_id": policy.resolve(entry)} for entry in config.alarm_sources
    ]
    # Per precondition: what it guards, then what it requires. A target that
    # does not resolve is worse than a requirement that does not — it stops all
    # control (preconditionUnresolved) — so it is reported first.
    check["preconditions"] = []
    for rule in config.preconditions:
        check["preconditions"].extend(
            {"entry": target, "node_id": policy.resolve(target)} for target in rule.targets
        )
        for object_id, method_id in rule.methods:
            halves = (policy.resolve(object_id), policy.resolve(method_id))
            check["preconditions"].append(
                {
                    "entry": f"{object_id}|{method_id}",
                    "node_id": None if None in halves else "|".join(halves),
                }
            )
        check["preconditions"].extend(
            {"entry": requirement["node"], "node_id": policy.resolve(requirement["node"])}
            for requirement in rule.require
        )
    return check


def check_policy(
    client: Any,
    policy: ToolPolicy,
    facts: NodeFacts,
    metadata: NodeMetadata,
    server_limits: dict[str, int | None],
    generation: int | None,
) -> dict[str, Any]:
    """Resolve the policy on this session, bind it, and report: ``serverStatus.policy_check``.

    Never raises. Each finding is printed as ``WARNING: policy check: <problem>``
    — the line an operator watching the startup log will see — and returned for
    ``get_server_status``. A failure partway leaves the bindings of whatever did
    resolve, and an unresolved deny_read refusing reads; nothing it could not
    finish is allowed.
    """
    operator = policy.config.profile == "operator"
    resolution = Resolution()
    findings: list[dict[str, str]] = []
    deny_bound = False
    try:
        _resolve_paths(client, _path_entries(policy, operator), resolution)
        policy.bind_paths({path: found.node_id for path, found in resolution.paths.items()})
        if operator:
            _expand_subtrees(client, policy, server_limits, resolution)
        policy.bind_subtrees(resolution.subtree_bounds)
        _expand_deny(client, policy, server_limits, resolution)
        policy.bind_deny(resolution.deny_nodes, resolution.deny_complete, resolution.deny_reason)
        deny_bound = True

        check: dict[str, Any] = {"deny": resolution.deny}
        if operator:
            check.update(_lint_input(client, policy, facts, metadata, resolution))
        findings = policy_findings(check)
    except Exception as error:
        reason = describe_error(error)
        print(f"Policy check failed: {reason}", file=sys.stderr)
        if policy.config.deny_read and not deny_bound:
            # The deny set was never built on this session; reads wait for one.
            entry = policy.config.deny_read[0]
            problem = finding_message("denyFailed", entry=entry, reason=reason)
            policy.bind_deny(resolution.deny_nodes, False, _as_reason(problem))
            findings.append({"entry": entry, "problem": problem})
    for finding in findings:
        print(f"WARNING: policy check: {finding['problem']}", file=sys.stderr)

    count, complete = policy.read_policy
    return {
        "generation": generation,
        "writable_nodes": sorted(policy.writable_set()) if operator else [],
        "callable_methods": sorted(policy.callable_pairs()) if operator else [],
        "read_denied": count,
        "read_policy_complete": complete,
        "findings": findings,
    }


__all__ = ["PathResolution", "WalkFailed", "check_policy", "resolve_path", "walk"]
