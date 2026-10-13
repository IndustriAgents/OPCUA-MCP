// The policy resolved against one session's address space, and checked against it.
//
// `policy_resolution.py` is the Python half. Runs on every session — the first,
// a rebuilt one, one node-opcua re-created — after the NamespaceArray is bound,
// because everything here names nodes, and a restarted server may have put them
// somewhere else:
//
// 1. Browse-path entries are resolved, segment by segment from the Root folder,
//    and an ambiguous segment resolves to nothing: a path that could mean two
//    nodes must not quietly mean whichever the server listed first.
// 2. writable_subtrees rules are expanded into the Variables they match, and a
//    rule that matches more than its max_nodes contributes nothing at all — a
//    rule an operator wrote for forty tags that now matches four thousand is not
//    the rule they wrote.
// 3. deny_read entries are expanded into everything under them. One that cannot
//    be expanded in full leaves the read policy incomplete, and every guarded
//    read is refused until it can be.
// 4. The writable and method allowlists are checked against the nodes' own
//    attributes, and every finding is printed and kept for get_server_status.
//
// Best-effort: nothing here fails a connect, and a finding never widens or
// disables an entry. But every policy decision fails closed — an entry that does
// not resolve allows nothing, hides nothing and satisfies no interlock.
import {
  BrowseDirection,
  ClientSession,
  NodeClass,
  type BrowseResult,
  type ReferenceDescription,
} from "node-opcua-client";

import { browseAllReferences, browseNameMatches } from "./browse.js";
import { isConnectionError } from "./connection.js";
import { CONTRACT } from "./contract.js";
import { sentence } from "./errors.js";
import { canonicalNodeId } from "./node-ids.js";
import type { NodeFactsCache } from "./node-facts.js";
import type { NodeMetadata } from "./node-metadata.js";
import { browseChunk, type ServerOperationLimits } from "./operation-limits.js";
import {
  policyFindings,
  type DenyCheck,
  type Finding,
  type MethodCheck,
  type SubtreeCheck,
  type UnresolvedReason,
  type WritableCheck,
} from "./policy-check.js";
import { ToolPolicy, boundRecord, isBrowsePath, isEmptyBound, type ValueBound } from "./policy.js";
import { isGood } from "./status.js";

/** The Root folder, which a policy browse path is written from. */
const ROOT_FOLDER = "ns=0;i=84";

/** The status that means "there is no such node", which is an answer, not a failure. */
const NO_SUCH_NODE = "BadNodeIdUnknown";

/** `resultShapes.serverStatus.policy_check`. */
export interface PolicyCheckRecord {
  generation: number | null;
  writable_nodes: string[];
  callable_methods: string[];
  read_denied: number;
  read_policy_complete: boolean;
  findings: Finding[];
}

/** What one browse path resolved to, and where it stopped if it did not. */
export interface PathResolution {
  node_id: string | null;
  reason: UnresolvedReason | null;
  segment: string | null;
  parent: string | null;
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Resolve a policy browse path from the Root folder, refusing an ambiguous one.
 *
 * Matched as `browse_opcua_nodes` matches `browse_path` — `2:Name` in namespace
 * 2, a bare `Name` in any — with one difference that is the point: where the
 * tool takes the first child that matches, this counts them, and more than one
 * distinct node is no node. A tool call can show its caller what it found; a
 * policy entry resolved at connect time has nobody to show, and an allowlist
 * that picked one of two pumps by the order a server listed them in is an
 * allowlist nobody wrote. A connection error propagates; anything else is "does
 * not resolve".
 */
export async function resolvePolicyPath(
  session: ClientSession,
  path: string
): Promise<PathResolution> {
  const segments = path.split("/").filter((segment) => segment.length > 0);
  let current = ROOT_FOLDER;
  for (const segment of segments) {
    let references: ReferenceDescription[];
    try {
      references = await browseAllReferences(session, current);
    } catch (error) {
      if (isConnectionError(error)) throw error;
      return { node_id: null, reason: "unresolved", segment, parent: current };
    }
    const matches = new Set(
      references
        .filter((reference) =>
          browseNameMatches(segment, reference.browseName.namespaceIndex, reference.browseName.name)
        )
        .map((reference) => canonicalNodeId(reference.nodeId.toString()))
    );
    if (matches.size !== 1) {
      return {
        node_id: null,
        reason: matches.size === 0 ? "unresolved" : "pathAmbiguous",
        segment,
        parent: current,
      };
    }
    current = [...matches][0];
  }
  if (segments.length === 0)
    return { node_id: null, reason: "unresolved", segment: null, parent: null };
  return { node_id: current, reason: null, segment: null, parent: null };
}

/** One node's forward hierarchical references, or the status that refused them. */
type Browsed = { references: ReferenceDescription[] } | { status: string };

/** Browse a whole level of a walk in as few requests as the server allows.
 *
 * One Browse per chunk rather than one per node: a deny_read entry over a large
 * folder is thousands of nodes, and a round trip each would be minutes on a
 * remote plant. Continuation points are drained per node, as `browseAllReferences`
 * drains them, so a wide node is never quietly cut short.
 */
async function browseLevel(
  session: ClientSession,
  nodeIds: string[],
  serverLimits: ServerOperationLimits
): Promise<Browsed[]> {
  const size = browseChunk(serverLimits, CONTRACT.traversal.maxTypeDefinitionsPerRequest);
  const answers: Browsed[] = [];
  for (let start = 0; start < nodeIds.length; start += size) {
    const results: BrowseResult[] = await session.browse(
      nodeIds.slice(start, start + size).map((nodeId) => ({
        nodeId,
        browseDirection: BrowseDirection.Forward,
        referenceTypeId: "HierarchicalReferences",
        includeSubtypes: true,
        nodeClassMask: 0,
        resultMask: 63,
      }))
    );
    for (let result of results) {
      const references: ReferenceDescription[] = [];
      for (;;) {
        if (!isGood(result.statusCode)) break;
        references.push(...(result.references ?? []));
        const point = result.continuationPoint;
        if (!point || point.length === 0) break;
        result = await session.browseNext(point, false);
      }
      answers.push(isGood(result.statusCode) ? { references } : { status: result.statusCode.name });
    }
  }
  return answers;
}

/** How a walk ended. */
type WalkOutcome = "done" | "stopped" | "rootMissing" | { failed: string };

/** Walk the hierarchical subtree under `root`, breadth first, to the traversal depth.
 *
 * `visit` sees each node below the root once, with the reference that found it,
 * and stops the walk by returning false. Nothing is skipped but what has been
 * seen — not the Server object the browse tool leaves out, because a policy that
 * names a node means everything under it. A root the server does not have is
 * `rootMissing`; a node below it that will not browse fails the walk when
 * `strict` (deny_read: a node that could not be listed may have hidden ones
 * under it) and is passed over otherwise (writable_subtrees: what did match is
 * still exactly what the rule says).
 */
async function walk(
  session: ClientSession,
  root: string,
  serverLimits: ServerOperationLimits,
  strict: boolean,
  visit: (nodeId: string, reference: ReferenceDescription) => boolean
): Promise<WalkOutcome> {
  const visited = new Set<string>([root]);
  let level = [root];
  for (let depth = 0; depth < CONTRACT.traversal.maxDepth && level.length > 0; depth++) {
    const browsed = await browseLevel(session, level, serverLimits);
    const next: string[] = [];
    for (const answer of browsed) {
      if ("status" in answer) {
        if (depth === 0) {
          return answer.status === NO_SUCH_NODE ? "rootMissing" : { failed: answer.status };
        }
        if (strict) return { failed: answer.status };
        continue;
      }
      for (const reference of answer.references) {
        const child = canonicalNodeId(reference.nodeId.toString());
        if (visited.has(child)) continue;
        visited.add(child);
        if (!visit(child, reference)) return "stopped";
        next.push(child);
      }
    }
    level = next;
  }
  return "done";
}

/** Each node's HasTypeDefinition target, canonical, or null when it has not exactly one.
 *
 * A browse of its own, batched, rather than `ReferenceDescription.TypeDefinition`
 * off the walk: that field is the server's copy, made when the reference was,
 * and a server may leave it empty or let it go stale when a node is retyped —
 * python-opcua does exactly that. The browse tool reads `type_definition` the
 * same way, and for the same reason the two must agree on what a node is.
 */
async function typeDefinitionsOf(
  session: ClientSession,
  nodeIds: string[],
  serverLimits: ServerOperationLimits
): Promise<Map<string, string | null>> {
  const types = new Map<string, string | null>();
  const size = browseChunk(serverLimits, CONTRACT.traversal.maxTypeDefinitionsPerRequest);
  for (let start = 0; start < nodeIds.length; start += size) {
    const part = nodeIds.slice(start, start + size);
    const results: BrowseResult[] = await session.browse(
      part.map((nodeId) => ({
        nodeId,
        browseDirection: BrowseDirection.Forward,
        referenceTypeId: CONTRACT.traversal.hasTypeDefinitionNodeId,
        // No subtypes: HasTypeDefinition has none, as fillTypeDefinitions says.
        includeSubtypes: false,
        nodeClassMask: 0,
        resultMask: 63,
      }))
    );
    results.forEach((result, index) => {
      const references = isGood(result.statusCode) ? (result.references ?? []) : [];
      types.set(
        part[index],
        references.length === 1 ? canonicalNodeId(references[0].nodeId.toString()) : null
      );
    });
  }
  return types;
}

/** What `checkPolicy` works with. */
export interface PolicyContext {
  session: ClientSession;
  policy: ToolPolicy;
  facts: NodeFactsCache;
  metadata: NodeMetadata;
  serverLimits: ServerOperationLimits;
  generation: number | null;
}

/** Every entry the policy names that applies under its profile, as written.
 *
 * Only deny_read outside `operator`: the control rules apply under no other
 * profile, so resolving them would be browsing the plant for nothing.
 */
function namedEntries(policy: ToolPolicy): string[] {
  const config = policy.config;
  if (config.profile !== "operator") return [...config.denyRead];
  return [
    ...config.writableNodes,
    ...[...config.callableMethods].flatMap((entry) => entry.split("|")),
    ...config.writableSubtrees.map((rule) => rule.root),
    ...config.denyRead,
    ...config.alarmSources,
    ...config.preconditions.flatMap((precondition) => [
      ...precondition.targets,
      ...precondition.methods.flatMap((pair) => [pair.object_id, pair.method_id]),
      ...precondition.require.map((requirement) => requirement.node),
    ]),
  ];
}

/** Expand writable_subtrees, bind the matches, and say how each rule came out. */
async function expandSubtrees(context: PolicyContext): Promise<SubtreeCheck[]> {
  const { session, policy, serverLimits } = context;
  const matched = new Map<string, ValueBound>();
  const checks: SubtreeCheck[] = [];
  for (const rule of policy.config.writableSubtrees) {
    const check = (outcome: SubtreeCheck["outcome"]) =>
      checks.push({ entry: rule.root, outcome, limit: rule.maxNodes });
    const root = policy.resolveEntry(rule.root);
    if (root === null) {
      check("unresolved");
      continue;
    }
    // Exact, no subtypes: a rule for AnalogItemType must not also select every
    // subtype a vendor has derived from it, which the operator never saw.
    const type = rule.typeDefinition === null ? null : policy.resolveNodeId(rule.typeDefinition);
    if (rule.typeDefinition !== null && type === null) {
      check("empty");
      continue;
    }
    const matches: string[] = [];
    let outcome: WalkOutcome;
    try {
      // Without a type every Variable matches, and the walk can stop at the cap.
      // With one, the Variables are collected and their types asked separately.
      const variables: string[] = [];
      outcome = await walk(session, root, serverLimits, false, (nodeId, reference) => {
        if (reference.nodeClass !== NodeClass.Variable) return true;
        (type === null ? matches : variables).push(nodeId);
        return matches.length <= rule.maxNodes;
      });
      if (type !== null && typeof outcome === "string" && outcome !== "rootMissing") {
        const types = await typeDefinitionsOf(session, variables, serverLimits);
        matches.push(...variables.filter((nodeId) => types.get(nodeId) === type));
        if (matches.length > rule.maxNodes) outcome = "stopped";
      }
    } catch (error) {
      if (isConnectionError(error)) throw error;
      outcome = { failed: describeError(error) };
    }
    if (outcome === "rootMissing" || typeof outcome === "object") check("unresolved");
    else if (outcome === "stopped") check("tooLarge");
    else if (matches.length === 0) check("empty");
    else {
      check("ok");
      for (const nodeId of matches) {
        if (!matched.has(nodeId)) matched.set(nodeId, rule.bound);
      }
    }
  }
  policy.bindSubtrees(matched);
  return checks;
}

/** A finding's problem as the reason inside readPolicyIncomplete, which adds its
 *  own full stop. */
function asReason(problem: string): string {
  return problem.replace(/\.$/, "");
}

/** Expand deny_read, bind the set, and say how each entry came out. */
async function expandDenied(context: PolicyContext): Promise<DenyCheck[]> {
  const { session, policy, serverLimits } = context;
  const cap = CONTRACT.policyCheck.maxDenyNodes;
  const denied = new Set<string>();
  const checks: DenyCheck[] = [];
  let incomplete: string | null = null;
  for (const entry of policy.config.denyRead) {
    const root = policy.resolveEntry(entry);
    if (root === null) {
      checks.push({ entry, outcome: "unresolved", limit: cap, reason: null });
      continue;
    }
    const hadRoot = denied.has(root);
    denied.add(root);
    let outcome: WalkOutcome;
    try {
      outcome =
        denied.size > cap
          ? "stopped"
          : await walk(session, root, serverLimits, true, (nodeId) => {
              denied.add(nodeId);
              return denied.size <= cap;
            });
    } catch (error) {
      if (isConnectionError(error)) throw error;
      outcome = { failed: describeError(error) };
    }
    if (outcome === "rootMissing") {
      if (!hadRoot) denied.delete(root);
      checks.push({ entry, outcome: "unresolved", limit: cap, reason: null });
    } else if (outcome === "stopped") {
      checks.push({ entry, outcome: "tooLarge", limit: cap, reason: null });
      incomplete ??= asReason(sentence("policyCheck", "denyTooLarge", { entry, limit: cap }));
      // Past the cap every read is refused already, and walking the rest would
      // only cost the plant more browsing to say the same thing.
      break;
    } else if (typeof outcome === "object") {
      checks.push({ entry, outcome: "failed", limit: cap, reason: outcome.failed });
      incomplete ??= asReason(
        sentence("policyCheck", "denyFailed", { entry, reason: outcome.failed })
      );
    } else {
      checks.push({ entry, outcome: "ok", limit: cap, reason: null });
    }
  }
  policy.bindDenied(denied, incomplete);
  return checks;
}

/** The writable entries, resolved and with what their nodes say (operator only). */
async function writableChecks(
  context: PolicyContext,
  paths: Map<string, PathResolution>
): Promise<WritableCheck[]> {
  const { session, policy, facts, metadata } = context;
  const entries = [...policy.config.writableNodes];
  const resolved = entries.map((entry) => policy.resolveEntry(entry));
  const ids = resolved.filter((nodeId): nodeId is string => nodeId !== null);
  const known = await facts.forNodes(session, ids);
  const engineering = await metadata.forNodes(session, ids);
  return entries.map((entry, index) => {
    const nodeId = resolved[index];
    const path = paths.get(entry);
    const bound = policy.config.valueBounds.get(entry) ?? null;
    return {
      entry,
      node_id: nodeId,
      unresolved_reason: nodeId === null ? (path?.reason ?? "unresolved") : null,
      segment: nodeId === null ? (path?.segment ?? null) : null,
      parent: nodeId === null ? (path?.parent ?? null) : null,
      bound: bound === null || isEmptyBound(bound) ? null : boundRecord(bound),
      facts: nodeId === null ? null : (known.get(nodeId) ?? null),
      engineering: nodeId === null ? null : (engineering.get(nodeId) ?? null),
    };
  });
}

/** The callable_methods entries, resolved and checked (operator only). */
async function methodChecks(
  context: PolicyContext,
  paths: Map<string, PathResolution>
): Promise<MethodCheck[]> {
  const { session, policy, facts } = context;
  const entries = [...policy.config.callableMethods];
  const pairs = entries.map((entry) => {
    const [object, method] = entry.split("|");
    return {
      entry,
      object,
      method,
      objectId: policy.resolveEntry(object),
      methodId: policy.resolveEntry(method),
    };
  });
  const ids = pairs.flatMap((pair) =>
    [pair.objectId, pair.methodId].filter((nodeId): nodeId is string => nodeId !== null)
  );
  const known = await facts.forNodes(session, ids);
  const checks: MethodCheck[] = [];
  for (const pair of pairs) {
    const failedHalf =
      pair.objectId === null ? pair.object : pair.methodId === null ? pair.method : null;
    const path = failedHalf === null ? undefined : paths.get(failedHalf);
    const bothResolved = pair.objectId !== null && pair.methodId !== null;
    checks.push({
      entry: pair.entry,
      object_node_id: pair.objectId,
      method_node_id: pair.methodId,
      unresolved_reason: bothResolved ? null : (path?.reason ?? "unresolved"),
      segment: bothResolved ? null : (path?.segment ?? null),
      parent: bothResolved ? null : (path?.parent ?? null),
      object_facts: pair.objectId === null ? null : (known.get(pair.objectId) ?? null),
      method_facts: pair.methodId === null ? null : (known.get(pair.methodId) ?? null),
      on_object: bothResolved
        ? await facts.onObject(session, pair.objectId as string, pair.methodId as string)
        : null,
    });
  }
  return checks;
}

/** Resolve the policy on this session, bind what it resolved to, and check it.
 *
 * In the order the spec gives, each step reading what the one before bound:
 * paths, subtrees, the deny set, then the lint. A connection error propagates,
 * leaving whatever was bound before it — which `forgetResolution` has already
 * emptied, so the policy is closed until the next session tries again.
 */
export async function checkPolicy(context: PolicyContext): Promise<PolicyCheckRecord> {
  const { session, policy } = context;
  const operator = policy.config.profile === "operator";

  const paths = new Map<string, PathResolution>();
  for (const entry of new Set(namedEntries(policy).filter(isBrowsePath))) {
    paths.set(entry, await resolvePolicyPath(session, entry));
  }
  policy.bindPaths(new Map([...paths].map(([entry, resolution]) => [entry, resolution.node_id])));

  // The control rules are operator policy, and are resolved and checked only
  // where they apply; deny_read applies under every profile.
  const subtrees = operator ? await expandSubtrees(context) : [];
  if (!operator) policy.bindSubtrees(new Map());
  const deny = await expandDenied(context);

  const findings = policyFindings({
    writable: operator ? await writableChecks(context, paths) : [],
    methods: operator ? await methodChecks(context, paths) : [],
    subtrees,
    deny,
    alarm_sources: operator
      ? policy.config.alarmSources.map((entry) => ({ entry, node_id: policy.resolveEntry(entry) }))
      : [],
    // Per precondition: its targets, its method pairs, its requirement nodes. A
    // target or pair that does not resolve stops all control (spec §12), and is
    // reported here so the operator can see why.
    preconditions: operator
      ? policy.config.preconditions.flatMap((precondition) => [
          ...precondition.targets.map((target) => ({
            entry: target,
            node_id: policy.resolveEntry(target),
          })),
          ...precondition.methods.map((pair) => {
            const object = policy.resolveEntry(pair.object_id);
            const method = policy.resolveEntry(pair.method_id);
            return {
              entry: `${pair.object_id}|${pair.method_id}`,
              node_id: object === null || method === null ? null : `${object}|${method}`,
            };
          }),
          ...precondition.require.map((requirement) => ({
            entry: requirement.node,
            node_id: policy.resolveEntry(requirement.node),
          })),
        ])
      : [],
  });
  for (const found of findings) {
    console.error(`WARNING: policy check: ${found.problem}`);
  }

  const denyState = policy.denyState();
  return {
    generation: context.generation,
    writable_nodes: operator ? [...policy.writableSet()].sort() : [],
    callable_methods: operator ? [...policy.callableSet()].sort() : [],
    read_denied: denyState.size,
    read_policy_complete: denyState.complete,
    findings,
  };
}
