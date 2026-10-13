// The policy measured against the plant: the check on connect, interlocks, alarm scope.
//
// `policy_check.py` is the Python half. Everything here is pure and driven by a
// shared table — `policy-check.json`, `preconditions.json`, `alarm-scope.json` —
// and everything here is a *policy* decision, so it fails closed: an entry that
// does not resolve allows nothing, hides nothing and satisfies no interlock,
// where a plant fact that could not be read (node-facts.ts) is a check skipped.
// The networked half — resolving entries, walking subtrees, reading values — is
// in policy-resolution.ts and the tool path.
import { message, sentence } from "./errors.js";
import { CURRENT_WRITE, factsGood, lacksBit, type NodeFacts } from "./node-facts.js";
import type { AnalogInfo } from "./node-metadata.js";
import { asNumber, formatNumber, sameJsonValue } from "./policy.js";
import { statesText, valueText, type BoundRecord } from "./write-plan.js";

// --- the check on connect (spec §7) ---------------------------------------------

/** Why an entry did not resolve: no match, or more than one. */
export type UnresolvedReason = "unresolved" | "pathAmbiguous";

export interface WritableCheck {
  entry: string;
  node_id: string | null;
  unresolved_reason: UnresolvedReason | null;
  segment: string | null;
  parent: string | null;
  bound: BoundRecord | null;
  facts: NodeFacts | null;
  engineering: AnalogInfo | null;
}

export interface MethodCheck {
  entry: string;
  object_node_id: string | null;
  method_node_id: string | null;
  unresolved_reason: UnresolvedReason | null;
  segment?: string | null;
  parent?: string | null;
  object_facts: NodeFacts | null;
  method_facts: NodeFacts | null;
  on_object: boolean | null;
}

export interface SubtreeCheck {
  entry: string;
  outcome: "ok" | "empty" | "tooLarge" | "unresolved";
  limit: number;
}

export interface DenyCheck {
  entry: string;
  outcome: "ok" | "unresolved" | "tooLarge" | "failed";
  limit: number;
  reason: string | null;
}

export interface ResolvedEntry {
  entry: string;
  node_id: string | null;
}

export interface PolicyCheckInput {
  writable: WritableCheck[];
  methods: MethodCheck[];
  subtrees: SubtreeCheck[];
  deny: DenyCheck[];
  alarm_sources: ResolvedEntry[];
  preconditions: ResolvedEntry[];
}

export interface Finding {
  entry: string;
  problem: string;
}

function finding(entry: string, key: string, fields: Record<string, string | number>): Finding {
  return { entry, problem: sentence("policyCheck", key, { entry, ...fields }) };
}

/** The finding for an entry that resolved to nothing. */
function unresolvedFinding(
  entry: string,
  reason: UnresolvedReason | null,
  segment?: string | null,
  parent?: string | null
): Finding {
  return reason === "pathAmbiguous"
    ? finding(entry, "pathAmbiguous", { segment: segment ?? "", parent: parent ?? "" })
    : finding(entry, "unresolved", {});
}

/** What one writable entry is told, in order; see `policyFindings`. */
function writableFindings(check: WritableCheck): Finding[] {
  const { entry, facts } = check;
  if (check.node_id === null) {
    return [unresolvedFinding(entry, check.unresolved_reason, check.segment, check.parent)];
  }
  const nodeId = check.node_id;
  if (facts === null) return [];
  if (facts.status !== "Good") {
    return [finding(entry, "unknownNode", { node_id: nodeId, status: facts.status })];
  }
  if (facts.node_class !== "Variable") {
    return [
      finding(entry, "notVariable", { node_id: nodeId, node_class: facts.node_class ?? "unknown" }),
    ];
  }
  const found: Finding[] = [];
  // One or the other: a node read-only for everyone is not also worth saying is
  // read-only for this user.
  if (lacksBit(facts.access_level, CURRENT_WRITE)) {
    found.push(finding(entry, "readOnly", { node_id: nodeId }));
  } else if (lacksBit(facts.user_access_level, CURRENT_WRITE)) {
    found.push(finding(entry, "readOnlyForUser", { node_id: nodeId }));
  }
  const bound = check.bound;
  const range = check.engineering?.eu_range ?? null;
  if (bound && range) {
    const fields = { low: formatNumber(range.low), high: formatNumber(range.high) };
    if (bound.min !== null && bound.min < range.low) {
      found.push(
        finding(entry, "boundOutsideRange", {
          key: "min",
          value: formatNumber(bound.min),
          ...fields,
        })
      );
    }
    if (bound.max !== null && bound.max > range.high) {
      found.push(
        finding(entry, "boundOutsideRange", {
          key: "max",
          value: formatNumber(bound.max),
          ...fields,
        })
      );
    }
  }
  if (bound?.enum && facts.states) {
    const states = facts.states;
    for (const value of bound.enum) {
      const defined =
        typeof value === "number"
          ? states.some((state) => state.value === value)
          : typeof value === "string" && states.some((state) => state.label === value);
      if (!defined) {
        found.push(
          finding(entry, "enumNotAState", { value: valueText(value), allowed: statesText(states) })
        );
      }
    }
  }
  return found;
}

/** The first thing wrong with one callable_methods entry, if anything. */
function methodFinding(check: MethodCheck): Finding | null {
  const { entry } = check;
  if (check.object_node_id === null || check.method_node_id === null) {
    return unresolvedFinding(entry, check.unresolved_reason, check.segment, check.parent);
  }
  const object = check.object_facts;
  const method = check.method_facts;
  if (object !== null && object.status !== "Good") {
    return finding(entry, "unknownNode", { node_id: check.object_node_id, status: object.status });
  }
  if (method !== null && method.status !== "Good") {
    return finding(entry, "unknownNode", { node_id: check.method_node_id, status: method.status });
  }
  if (factsGood(method) && method.node_class !== "Method") {
    return finding(entry, "methodNotAMethod", {
      method_node_id: check.method_node_id,
      node_class: method.node_class ?? "unknown",
    });
  }
  if (factsGood(object) && object.node_class !== "Object" && object.node_class !== "ObjectType") {
    return finding(entry, "methodObjectNotAnObject", {
      object_node_id: check.object_node_id,
      node_class: object.node_class ?? "unknown",
    });
  }
  if (method?.executable === false) return finding(entry, "methodNotExecutable", {});
  if (method?.user_executable === false) return finding(entry, "methodNotExecutableForUser", {});
  if (check.on_object === false) {
    return finding(entry, "methodNotOnObject", {
      method_node_id: check.method_node_id,
      object_node_id: check.object_node_id,
    });
  }
  return null;
}

/** Everything the policy names that this server says cannot work (spec §7).
 *
 * In policy order — writable, methods, subtrees, deny, alarm sources,
 * preconditions — and within each in the order written. A writable entry stops
 * at the first finding that makes the rest meaningless (it does not resolve, the
 * server does not have it, it is not a Variable) and otherwise reports all that
 * apply; a method entry reports its first. A finding never changes what the
 * policy allows: the entry already allows nothing the server will accept, and
 * saying so is the point.
 */
export function policyFindings(input: PolicyCheckInput): Finding[] {
  const found: Finding[] = [];
  for (const check of input.writable) found.push(...writableFindings(check));
  for (const check of input.methods) {
    const problem = methodFinding(check);
    if (problem) found.push(problem);
  }
  for (const check of input.subtrees) {
    if (check.outcome === "empty") found.push(finding(check.entry, "subtreeEmpty", {}));
    if (check.outcome === "tooLarge") {
      found.push(finding(check.entry, "subtreeTooLarge", { limit: check.limit }));
    }
    if (check.outcome === "unresolved") found.push(finding(check.entry, "unresolved", {}));
  }
  for (const check of input.deny) {
    if (check.outcome === "unresolved") found.push(finding(check.entry, "unresolved", {}));
    if (check.outcome === "tooLarge") {
      found.push(finding(check.entry, "denyTooLarge", { limit: check.limit }));
    }
    if (check.outcome === "failed") {
      found.push(finding(check.entry, "denyFailed", { reason: check.reason ?? "unknown" }));
    }
  }
  for (const check of [...input.alarm_sources, ...input.preconditions]) {
    if (check.node_id === null) found.push(finding(check.entry, "unresolved", {}));
  }
  return found;
}

// --- preconditions (spec §6.5) ----------------------------------------------------

/** One `require` item of a precondition, as written in the policy file. */
export interface Requirement {
  node: string;
  equals?: string | number | boolean | null;
  in?: Array<string | number | boolean> | null;
  min?: number | null;
  max?: number | null;
}

/** What reading one requirement node returned; null when it did not resolve. */
export type RequirementReading = { status: string; value?: unknown } | null;

/** A requirement as the refusal states it: `ns=2;i=110 equals true`. */
function requirementText(requirement: Requirement): string {
  const node = requirement.node;
  if (requirement.equals !== undefined && requirement.equals !== null) {
    return `${node} equals ${valueText(requirement.equals)}`;
  }
  if (requirement.in !== undefined && requirement.in !== null) {
    return `${node} is one of ${requirement.in.map(valueText).join(", ")}`;
  }
  const min = requirement.min ?? null;
  const max = requirement.max ?? null;
  if (min !== null && max !== null) {
    return `${node} is between ${formatNumber(min)} and ${formatNumber(max)}`;
  }
  if (min !== null) return `${node} is at least ${formatNumber(min)}`;
  return `${node} is at most ${formatNumber(max ?? 0)}`;
}

/** Whether a requirement holds on what was read. Only a Good value can hold. */
function holds(requirement: Requirement, reading: RequirementReading): boolean {
  if (reading === null || reading.status !== "Good") return false;
  const value = reading.value;
  if (requirement.equals !== undefined && requirement.equals !== null) {
    return sameJsonValue(value, requirement.equals);
  }
  if (requirement.in !== undefined && requirement.in !== null) {
    return requirement.in.some((candidate) => sameJsonValue(value, candidate));
  }
  const number = asNumber(value);
  if (number === null) return false;
  const min = requirement.min ?? null;
  const max = requirement.max ?? null;
  return (min === null || number >= min) && (max === null || number <= max);
}

function readingText(reading: RequirementReading): string {
  if (reading === null) return "unresolvable";
  if (reading.status !== "Good") return `unreadable (${reading.status})`;
  return valueText(reading.value);
}

/** null when every requirement holds, else the refusal for the first that does not.
 *
 * `target` is what is being changed, as the caller named it — a node id, or
 * `object|method` — and `current` maps each requirement node, as written, to what
 * reading it returned. Pure; `tests/fixtures/preconditions.json` pins it.
 */
export function checkPreconditions(input: {
  target: string;
  requirements: Requirement[];
  current: Record<string, RequirementReading | undefined>;
}): string | null {
  for (const requirement of input.requirements) {
    const reading = input.current[requirement.node] ?? null;
    if (holds(requirement, reading)) continue;
    return message("preconditionNotMet", {
      target: input.target,
      requirement: requirementText(requirement),
      node_id: requirement.node,
      current: readingText(reading),
    });
  }
  return null;
}

// --- alarm scope (spec §6.4) -------------------------------------------------------

/** null when the operator policy lets this condition be acted on, else why not.
 *
 * `sources` null and `max_severity` null mean "not configured"; `error` is why
 * the condition's SourceNode or Severity could not be read, and refuses: an alarm
 * whose scope cannot be checked is not acknowledged on the policy's behalf. Pure;
 * `tests/fixtures/alarm-scope.json` pins it.
 */
export function checkAlarmScope(input: {
  condition_id: string;
  source: string | null;
  severity: number | null;
  error: string | null;
  sources: string[] | null;
  max_severity: number | null;
}): string | null {
  const conditionId = input.condition_id;
  if (input.error !== null) {
    return message("alarmScopeUnreadable", { condition_id: conditionId, reason: input.error });
  }
  if (input.sources !== null && (input.source === null || !input.sources.includes(input.source))) {
    return message("alarmSourceNotAllowed", {
      condition_id: conditionId,
      source_node_id: input.source ?? "an unknown source",
    });
  }
  if (
    input.max_severity !== null &&
    (input.severity === null || input.severity > input.max_severity)
  ) {
    return message("alarmTooSevere", {
      condition_id: conditionId,
      severity: input.severity === null ? "unknown" : formatNumber(input.severity),
      limit: formatNumber(input.max_severity),
    });
  }
  return null;
}
