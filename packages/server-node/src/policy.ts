import { readFileSync } from "fs";

import { ACCESS_CLASSES } from "./access-classes.js";
import { CONTRACT, type ToolGuard, type ToolSpec } from "./contract.js";
import { namespaceUriForm, resolveNodeId } from "./node-ids.js";
import { ContractRefusal, message } from "./errors.js";
import { parseNumericString } from "./numeric.js";
import type { Requirement } from "./policy-check.js";
import type { BoundRecord } from "./write-plan.js";

export type ToolProfile = "observe" | "operator" | "full";

/** What the control gate can say, and the one word every surface uses for it:
 * the startup line, `get_server_status` and every audit record. Named apart
 * because "control is on" is not one fact — a verified server and a lab override
 * both turn it on, and a reviewer has to be able to tell which did. */
export type ControlGate = "secured" | "INSECURE-OVERRIDE" | "UNVERIFIED-OVERRIDE" | "blocked";

/** One `writable_nodes` entry when it carries a bound rather than only a node. */
interface WritableNodeEntry {
  node: string;
  min?: number;
  max?: number;
  enum?: unknown[] | null;
  max_change?: number;
}

/** One `writable_subtrees` rule as written. */
interface WritableSubtreeEntry {
  root: string;
  type_definition?: string | null;
  max_nodes?: number | null;
  min?: number | null;
  max?: number | null;
  enum?: unknown[] | null;
  max_change?: number | null;
}

/** One `preconditions` entry as written. */
interface PreconditionEntry {
  targets?: string[] | null;
  methods?: Array<{ object_id: string; method_id: string }> | null;
  require: Requirement[];
  description?: string | null;
}

/** A policy file once `checkPolicyFile` has passed it. `null` is "not set". */
interface PolicyFile {
  version?: number | null;
  profile?: string | null;
  allowed_tools?: string[] | null;
  allow_insecure_control?: boolean | null;
  allow_unverified_server_control?: boolean | null;
  allow_out_of_range_writes?: boolean | null;
  deny_read?: string[] | null;
  control?: {
    writable_nodes?: Array<string | WritableNodeEntry> | null;
    callable_methods?: Array<{ object_id: string; method_id: string }> | null;
    acknowledge_alarms?: boolean | null;
    writable_subtrees?: WritableSubtreeEntry[] | null;
    alarm_sources?: string[] | null;
    alarm_max_severity?: number | null;
    preconditions?: PreconditionEntry[] | null;
  } | null;
}

/** What an allowlisted node may be written, beyond being the right node.
 *
 * Node identity was the whole of write authorization, and it is the weakest link
 * in the safety story: a model that correctly identified the right setpoint and
 * hallucinated `9999` instead of `99.9` was fully authorized. The variant codec
 * range-checks integers and refuses a lossy Int64, but that is *type* safety —
 * `9999` is a perfectly good Double.
 *
 * `minimum`, `maximum` and `allowed` are checked by `ToolPolicy.authorize`,
 * before the OPC UA network is touched at all. `maxChange` cannot be: it is a
 * bound on the *move*, so it needs the node's current value, and it is enforced
 * in the write path where that read already happens.
 */
export interface ValueBound {
  minimum: number | null;
  maximum: number | null;
  /** The only values this node accepts. Numbers, strings or booleans — a
   *  discrete node is usually the latter two. */
  allowed: readonly unknown[] | null;
  /** The largest absolute difference from the node's current value one write may
   *  make. */
  maxChange: number | null;
}

const EMPTY_BOUND: ValueBound = {
  minimum: null,
  maximum: null,
  allowed: null,
  maxChange: null,
};

export function isEmptyBound(bound: ValueBound): boolean {
  return (
    bound.minimum === null &&
    bound.maximum === null &&
    bound.allowed === null &&
    bound.maxChange === null
  );
}

const BOUND_KEYS = ["node", "min", "max", "enum", "max_change"];
const SUBTREE_KEYS = ["root", "type_definition", "max_nodes", "min", "max", "enum", "max_change"];

/** A writable entry's bound keys as a `ValueBound`, or a refusal naming the entry.
 *
 * Shared by `writable_nodes` objects and `writable_subtrees` rules, which carry
 * the same four keys and are refused in the same words but for what the entry is
 * called. In one order — min, max, enum, max_change, then the two relations — so
 * a rule with two faults reports the same one on both runtimes.
 */
function boundOf(entry: Record<string, unknown>, label: string, name: string): ValueBound {
  const number = (key: "min" | "max" | "max_change"): number | null => {
    const value = entry[key];
    if (value === undefined || value === null) return null;
    // Finite as well: `1e400` parses to infinity, which is not a bound anyone wrote.
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new Error(`${label} entry "${name}" has a non-numeric ${key}`);
    }
    return value;
  };
  const minimum = number("min");
  const maximum = number("max");
  // `null` is "no enum", as it is "no bound" for min and max. This refused it
  // while Python accepted it (#157).
  const values = entry.enum ?? null;
  if (values !== null && (!Array.isArray(values) || values.length === 0)) {
    throw new Error(`${label} entry "${name}" has an empty or non-list enum`);
  }
  const bound: ValueBound = {
    minimum,
    maximum,
    allowed: values as unknown[] | null,
    maxChange: number("max_change"),
  };
  if (bound.minimum !== null && bound.maximum !== null && bound.minimum > bound.maximum) {
    throw new Error(`${label} entry "${name}" has min above max`);
  }
  if (bound.maxChange !== null && bound.maxChange < 0) {
    throw new Error(`${label} entry "${name}" has a negative max_change`);
  }
  return bound;
}

/** The first key of `entry` not in `known`, in sorted order, or undefined. */
function unknownKey(entry: Record<string, unknown>, known: readonly string[]): string | undefined {
  return Object.keys(entry)
    .filter((key) => !known.includes(key))
    .sort()[0];
}

/** One `writable_nodes` entry as a [node id, bound] pair.
 *
 * A bare string stays legal and carries no bound, so every policy file written
 * before this existed keeps working and means exactly what it meant.
 */
function valueBound(entry: string | WritableNodeEntry): [string, ValueBound] {
  if (typeof entry === "string") return [entry, EMPTY_BOUND];
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    throw new Error("writable_nodes entry must be a node id or an object");
  }
  const unknown = unknownKey(entry as unknown as Record<string, unknown>, BOUND_KEYS);
  if (unknown !== undefined) {
    // Loud, because the failure it prevents is silent: an operator who writes
    // "minimum" instead of "min" believes a bound is in force and none is.
    throw new Error(
      `Unknown key in a writable_nodes entry: ${unknown}. ` +
        `Use one of: ${[...BOUND_KEYS].sort().join(", ")}`
    );
  }
  if (typeof entry.node !== "string" || entry.node.trim() === "") {
    throw new Error("A writable_nodes entry must name a node");
  }
  return [
    entry.node,
    boundOf(entry as unknown as Record<string, unknown>, "writable_nodes", entry.node),
  ];
}

/** One `writable_subtrees` rule: every Variable under `root` (of `typeDefinition`,
 *  when given) is writable with `bound`, up to `maxNodes` of them. */
export interface SubtreeRule {
  root: string;
  typeDefinition: string | null;
  maxNodes: number;
  bound: ValueBound;
}

/** One `writable_subtrees` entry, checked, as a rule (spec §6.2). */
function subtreeRule(entry: unknown): SubtreeRule {
  if (!isObject(entry)) {
    throw new Error("A control.writable_subtrees entry must be an object");
  }
  const unknown = unknownKey(entry, SUBTREE_KEYS);
  if (unknown !== undefined) {
    throw new Error(
      `Unknown key in a writable_subtrees entry: ${unknown}. ` +
        `Use one of: ${[...SUBTREE_KEYS].sort().join(", ")}`
    );
  }
  if (!nonEmpty(entry.root)) {
    throw new Error("A control.writable_subtrees entry must name a root");
  }
  const root = entry.root as string;
  const typeDefinition = entry.type_definition ?? null;
  if (typeDefinition !== null && !nonEmpty(typeDefinition)) {
    throw new Error(`writable_subtrees entry "${root}" has an empty or non-string type_definition`);
  }
  const limits = CONTRACT.policyCheck;
  const maxNodes = entry.max_nodes ?? null;
  if (
    maxNodes !== null &&
    !(
      typeof maxNodes === "number" &&
      Number.isInteger(maxNodes) &&
      maxNodes >= 1 &&
      maxNodes <= limits.maxSubtreeNodes
    )
  ) {
    throw new Error(
      `writable_subtrees entry "${root}" has max_nodes outside 1 to ${limits.maxSubtreeNodes}`
    );
  }
  return {
    root,
    typeDefinition: typeDefinition as string | null,
    maxNodes: (maxNodes as number | null) ?? limits.defaultSubtreeNodes,
    bound: boundOf(entry, "writable_subtrees", root),
  };
}

const PRECONDITION_KEYS = ["targets", "methods", "require", "description"];
const REQUIREMENT_KEYS = ["node", "equals", "in", "min", "max"];

/** A value a precondition may compare with: a string, a finite number, a boolean. */
function scalar(value: unknown): boolean {
  return (
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  );
}

/** One `preconditions` entry, checked (spec §6.5). */
function checkPrecondition(entry: unknown): void {
  if (!isObject(entry)) {
    throw new Error("A control.preconditions entry must be an object");
  }
  const unknown = unknownKey(entry, PRECONDITION_KEYS);
  if (unknown !== undefined) {
    throw new Error(
      `Unknown key in a control.preconditions entry: ${unknown}. ` +
        `Use one of: ${[...PRECONDITION_KEYS].sort().join(", ")}`
    );
  }
  const targets = entry.targets ?? null;
  if (targets !== null && !(Array.isArray(targets) && targets.every(nonEmpty))) {
    throw new Error(
      "A control.preconditions entry's targets must be a list of node IDs or browse paths"
    );
  }
  const methods = entry.methods ?? null;
  if (
    methods !== null &&
    !(
      Array.isArray(methods) &&
      methods.every(
        (item) => isObject(item) && nonEmpty(item.object_id) && nonEmpty(item.method_id)
      )
    )
  ) {
    throw new Error(
      "A control.preconditions entry's methods must be a list of objects with a non-empty object_id and method_id"
    );
  }
  if (
    ((targets as unknown[] | null)?.length ?? 0) + ((methods as unknown[] | null)?.length ?? 0) ===
    0
  ) {
    throw new Error("A control.preconditions entry must name at least one target or method");
  }
  const require = entry.require ?? null;
  if (!Array.isArray(require) || require.length === 0) {
    throw new Error("A control.preconditions entry must have a non-empty require list");
  }
  if ((entry.description ?? null) !== null && typeof entry.description !== "string") {
    throw new Error("A control.preconditions entry's description must be a string");
  }
  require.forEach(checkRequirement);
}

/** One `require` item: a node, and exactly one of equals, in, or min/max. */
function checkRequirement(requirement: unknown): void {
  if (!isObject(requirement)) {
    throw new Error("A precondition requirement must be an object");
  }
  const unknown = unknownKey(requirement, REQUIREMENT_KEYS);
  if (unknown !== undefined) {
    throw new Error(
      `Unknown key in a precondition requirement: ${unknown}. ` +
        `Use one of: ${[...REQUIREMENT_KEYS].sort().join(", ")}`
    );
  }
  if (!nonEmpty(requirement.node)) {
    throw new Error("A precondition requirement must name a node");
  }
  const node = requirement.node as string;
  const set = (key: string) => (requirement[key] ?? null) !== null;
  const kinds = [set("equals"), set("in"), set("min") || set("max")].filter(Boolean).length;
  if (kinds !== 1) {
    throw new Error(
      `Precondition requirement on "${node}" must have exactly one of equals, in, or min/max`
    );
  }
  if (set("equals") && !scalar(requirement.equals)) {
    throw new Error(
      `Precondition requirement on "${node}" has an equals that is not a string, number or boolean`
    );
  }
  if (
    set("in") &&
    !(Array.isArray(requirement.in) && requirement.in.length > 0 && requirement.in.every(scalar))
  ) {
    throw new Error(
      `Precondition requirement on "${node}" has an in that is not a non-empty list of strings, numbers or booleans`
    );
  }
  for (const key of ["min", "max"]) {
    const value = requirement[key] ?? null;
    if (value !== null && !(typeof value === "number" && Number.isFinite(value))) {
      throw new Error(`Precondition requirement on "${node}" has a non-numeric ${key}`);
    }
  }
  const min = (requirement.min ?? null) as number | null;
  const max = (requirement.max ?? null) as number | null;
  if (min !== null && max !== null && min > max) {
    throw new Error(`Precondition requirement on "${node}" has min above max`);
  }
}

/** A list of node ids or browse paths, as deny_read and alarm_sources take. */
function isEntryList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(nonEmpty);
}

/** One `preconditions` entry once checked. */
export interface Precondition {
  targets: string[];
  methods: Array<{ object_id: string; method_id: string }>;
  require: Requirement[];
}

/** Whether a policy entry is a browse path from the Root folder rather than an id. */
export function isBrowsePath(entry: string): boolean {
  return entry.startsWith("/");
}

/** A `ValueBound` as the fixtures and `write_access` spell it. */
export function boundRecord(bound: ValueBound | null): BoundRecord | null {
  if (bound === null) return null;
  return {
    min: bound.minimum,
    max: bound.maximum,
    enum: bound.allowed,
    max_change: bound.maxChange,
  };
}

/** What this process can prove about the other end of the channel (#134).
 *
 * Two properties that used to be one boolean. *Secured* means the traffic is
 * signed (and, under SignAndEncrypt, encrypted): nobody can read or forge it in
 * transit. *Authenticated* means the peer has been shown to be the intended
 * server. The control gate used to check the first while its safety meaning
 * needs the second — both client libraries encrypt happily to whatever
 * certificate the endpoint presents, so an attacker who can answer for the
 * endpoint gets an encrypted channel, and with it the control tools.
 *
 * Derived from configuration alone, and that is sound rather than optimistic: a
 * pinned certificate (`OPCUA_SERVER_CERT`) is the key the client encrypts the
 * handshake to, so a server without the matching private key cannot complete
 * it, and a pin outside its validity window refuses to connect at all (see
 * `pinnedCertificateProblem` in security.ts). Whenever there is a session, a pin
 * means the peer is the pinned server.
 *
 * `server_identity` in `policy.py` is the other half, and
 * `tests/fixtures/control-gate.json` holds both to the same answers.
 */
export interface ServerIdentity {
  /** A SecurityPolicy other than None: mode Sign or SignAndEncrypt. */
  channelSecured: boolean;
  serverAuthenticated: boolean;
  /** A trust-store method would be a third value; neither client library offers
   *  one both runtimes can use, so there is none yet. */
  authenticationMethod: "pin" | "none";
}

/** The channel's identity guarantees, from the security variables. */
export function serverIdentity(env: NodeJS.ProcessEnv): ServerIdentity {
  const secured = (value(env, "OPCUA_SECURITY_POLICY") ?? "None").toLowerCase() !== "none";
  // `secured &&`: a pin on an unsecured channel is refused at startup by
  // security.ts, but this must not read it as authentication if it ever gets
  // here — with no channel security the server presents no certificate at all.
  const pinned = secured && value(env, "OPCUA_SERVER_CERT") !== undefined;
  return {
    channelSecured: secured,
    serverAuthenticated: pinned,
    authenticationMethod: pinned ? "pin" : "none",
  };
}

export interface PolicyConfig {
  profile: ToolProfile;
  allowedTools: Set<string> | null;
  writableNodes: Set<string>;
  /** Per-node value bounds, keyed by the allowlist entry exactly as written.
   *  Resolved against the live NamespaceArray at check time, the same way the
   *  identity allowlist is, so an `nsu=` entry follows a renumbered server. */
  valueBounds: Map<string, ValueBound>;
  callableMethods: Set<string>;
  acknowledgeAlarms: boolean;
  /** Control over a channel with no security at all (SecurityPolicy None). */
  allowInsecureControl: boolean;
  /** Control over a secured channel to a server whose identity is unverified.
   *  Deliberately not folded into `allowInsecureControl`: encryption and peer
   *  authentication are independent properties, and an operator who accepted
   *  the one for a lab has not thereby accepted the other. */
  allowUnverifiedServerControl: boolean;
  serverIdentity: ServerIdentity;
  /** Whether a write outside the range the OPC UA server itself published
   *  (`EURange`) is allowed through. Refused by default: a bound the equipment
   *  declares is worth more than one a human retyped, and it is the only value
   *  bound that exists on a deployment with no policy file at all. */
  allowOutOfRangeWrites: boolean;
  /** Nodes no read may touch, with everything under them: node ids or browse
   *  paths, as written. Applies under every profile. */
  denyRead: string[];
  /** Operator: subtrees whose Variables are writable, with a bound each. */
  writableSubtrees: SubtreeRule[];
  /** Operator: the only alarm sources acknowledge_alarm and act_on_alarm may act
   *  on. Empty is no restriction. */
  alarmSources: string[];
  /** Operator: the most severe condition they may act on, or null for any. */
  alarmMaxSeverity: number | null;
  /** Operator: interlocks that must hold before a write or a call goes out. */
  preconditions: Precondition[];
}

function value(env: NodeJS.ProcessEnv, name: string): string | undefined {
  const raw = env[name]?.trim();
  return raw || undefined;
}

function parseBoolean(raw: string | undefined, name: string, fallback: boolean): boolean {
  if (raw === undefined) return fallback;
  if (["1", "true", "yes", "on"].includes(raw.toLowerCase())) return true;
  if (["0", "false", "no", "off"].includes(raw.toLowerCase())) return false;
  throw new Error(`${name} must be true or false, got "${raw}"`);
}

function parseCsv(raw: string | undefined): string[] | undefined {
  if (raw === undefined) return undefined;
  return raw
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseProfile(raw: string | undefined): ToolProfile {
  const normalized = (raw || "observe").toLowerCase();
  if (normalized === "read-only" || normalized === "readonly") return "observe";
  if (normalized === "observe" || normalized === "operator" || normalized === "full") {
    return normalized;
  }
  throw new Error(
    `Invalid OPCUA_PROFILE: "${raw}". Use one of: observe, read-only, operator, full`
  );
}

function loadPolicyFile(path: string | undefined): PolicyFile {
  if (!path) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(
      `Cannot read OPCUA_POLICY_FILE ${path}: ${error instanceof Error ? error.message : String(error)}`
    );
  }
  if (!isObject(parsed)) {
    throw new Error(`OPCUA_POLICY_FILE ${path} must contain a JSON object`);
  }
  checkPolicyFile(parsed);
  return parsed as PolicyFile;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Refuse a policy file whose fields are the wrong shape, before any is used.
 *
 * Every field, whether or not an environment variable overrides it: a broken
 * file must not start working-looking the day the override is removed. The
 * checks run in one fixed order so a file with two faults reports the same one
 * on both runtimes, and `null` means "not set" throughout — as it already did
 * for `min` and `max`.
 *
 * This used to be whatever each runtime's code happened to do with a surprise.
 * A string where a flag belonged was truthy on both, so `"allow_insecure_control":
 * "false"` *enabled* insecure control; a callable_methods entry missing its ids
 * became the allowlist entry `undefined|undefined` here and a traceback in
 * Python; `"control": "x"` was ignored here and refused there (#157).
 * `tests/fixtures/policy-file-validation.json` pins every rule for both.
 */
function checkPolicyFile(file: Record<string, unknown>): void {
  const version = file.version ?? null;
  if (version !== null && version !== 1) {
    throw new Error(`Unsupported OPC UA policy version ${JSON.stringify(version)}; expected 1`);
  }
  if ((file.profile ?? null) !== null && typeof file.profile !== "string") {
    throw new Error("OPCUA_POLICY_FILE profile must be a string");
  }
  const tools = file.allowed_tools ?? null;
  if (tools !== null && !(Array.isArray(tools) && tools.every((n) => typeof n === "string"))) {
    throw new Error("OPCUA_POLICY_FILE allowed_tools must be a list of tool names");
  }
  requireFlag(file, "allow_insecure_control", "allow_insecure_control");
  requireFlag(file, "allow_unverified_server_control", "allow_unverified_server_control");
  requireFlag(file, "allow_out_of_range_writes", "allow_out_of_range_writes");
  // Before control, and before returning for a file without one: deny_read is
  // top-level, because it applies under every profile.
  const deny = file.deny_read ?? null;
  if (deny !== null && !isEntryList(deny)) {
    throw new Error("OPCUA_POLICY_FILE deny_read must be a list of node IDs or browse paths");
  }

  const control = file.control ?? null;
  if (control === null) return;
  if (!isObject(control)) {
    throw new Error("OPCUA_POLICY_FILE control must be an object");
  }
  const writable = control.writable_nodes ?? null;
  if (writable !== null) {
    if (!Array.isArray(writable)) {
      throw new Error("OPCUA_POLICY_FILE control.writable_nodes must be a list");
    }
    writable.forEach((entry) => valueBound(entry as string | WritableNodeEntry));
  }
  const methods = control.callable_methods ?? null;
  if (methods !== null) {
    if (!Array.isArray(methods)) {
      throw new Error("OPCUA_POLICY_FILE control.callable_methods must be a list");
    }
    for (const entry of methods) {
      if (!isObject(entry) || !nonEmpty(entry.object_id) || !nonEmpty(entry.method_id)) {
        throw new Error(
          "A control.callable_methods entry must be an object with a non-empty object_id and method_id"
        );
      }
    }
  }
  requireFlag(control, "acknowledge_alarms", "control.acknowledge_alarms");

  const subtrees = control.writable_subtrees ?? null;
  if (subtrees !== null) {
    if (!Array.isArray(subtrees)) {
      throw new Error("OPCUA_POLICY_FILE control.writable_subtrees must be a list");
    }
    subtrees.forEach(subtreeRule);
  }
  const sources = control.alarm_sources ?? null;
  if (sources !== null && !isEntryList(sources)) {
    throw new Error(
      "OPCUA_POLICY_FILE control.alarm_sources must be a list of node IDs or browse paths"
    );
  }
  const severity = control.alarm_max_severity ?? null;
  if (
    severity !== null &&
    !(
      typeof severity === "number" &&
      Number.isInteger(severity) &&
      severity >= 1 &&
      severity <= 1000
    )
  ) {
    throw new Error(
      "OPCUA_POLICY_FILE control.alarm_max_severity must be a whole number from 1 to 1000"
    );
  }
  const preconditions = control.preconditions ?? null;
  if (preconditions !== null) {
    if (!Array.isArray(preconditions)) {
      throw new Error("OPCUA_POLICY_FILE control.preconditions must be a list");
    }
    preconditions.forEach(checkPrecondition);
  }
}

function requireFlag(section: Record<string, unknown>, key: string, name: string): void {
  const value = section[key] ?? null;
  if (value !== null && typeof value !== "boolean") {
    throw new Error(`OPCUA_POLICY_FILE ${name} must be true or false`);
  }
}

function nonEmpty(value: unknown): boolean {
  return typeof value === "string" && value.trim() !== "";
}

function validateToolNames(names: string[] | null | undefined): Set<string> | null {
  if (names === undefined || names === null) return null;
  const known = new Set(CONTRACT.tools.map((tool) => tool.name));
  for (const name of names) {
    if (!known.has(name)) {
      throw new Error(`Unknown tool in allowed_tools: ${name}`);
    }
  }
  return new Set(names);
}

/** Parse the deployment policy. Environment variables override the optional JSON file. */
export function parsePolicyConfig(env: NodeJS.ProcessEnv): PolicyConfig {
  const file = loadPolicyFile(value(env, "OPCUA_POLICY_FILE"));
  const control = file.control ?? {};
  const profile = parseProfile(value(env, "OPCUA_PROFILE") ?? file.profile ?? undefined);
  const allowedTools = validateToolNames(
    parseCsv(value(env, "OPCUA_ALLOWED_TOOLS")) ?? file.allowed_tools
  );
  // The environment variable is a comma-separated list of node ids and can carry
  // no bounds; a bounded node needs the policy file. Setting it replaces the
  // file's list outright rather than merging, as every other override here does
  // — a half-overridden allowlist is the kind of thing nobody can reason about
  // at three in the morning.
  const writableEntries: Array<string | WritableNodeEntry> =
    parseCsv(value(env, "OPCUA_ALLOWED_WRITE_NODES")) ?? control.writable_nodes ?? [];
  if (!Array.isArray(writableEntries)) {
    throw new Error("OPCUA_POLICY_FILE control.writable_nodes must be a list");
  }
  const valueBounds = new Map<string, ValueBound>(writableEntries.map(valueBound));
  const writableNodes = new Set(valueBounds.keys());

  const fileMethods = (control.callable_methods ?? []).map(
    ({ object_id, method_id }) => `${object_id}|${method_id}`
  );
  const callableMethods = new Set(parseCsv(value(env, "OPCUA_ALLOWED_METHODS")) ?? fileMethods);
  for (const method of callableMethods) {
    if (!method.includes("|")) {
      throw new Error(
        `Invalid OPCUA_ALLOWED_METHODS entry "${method}"; use object_node_id|method_node_id`
      );
    }
  }

  const acknowledgeAlarms = parseBoolean(
    value(env, "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS"),
    "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS",
    control.acknowledge_alarms ?? false
  );
  const allowInsecureControl = parseBoolean(
    value(env, "OPCUA_ALLOW_INSECURE_CONTROL"),
    "OPCUA_ALLOW_INSECURE_CONTROL",
    file.allow_insecure_control ?? false
  );
  const allowUnverifiedServerControl = parseBoolean(
    value(env, "OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL"),
    "OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL",
    file.allow_unverified_server_control ?? false
  );
  const allowOutOfRangeWrites = parseBoolean(
    value(env, "OPCUA_ALLOW_OUT_OF_RANGE_WRITES"),
    "OPCUA_ALLOW_OUT_OF_RANGE_WRITES",
    file.allow_out_of_range_writes ?? false
  );
  return {
    profile,
    allowedTools,
    writableNodes,
    valueBounds,
    callableMethods,
    acknowledgeAlarms,
    allowInsecureControl,
    allowUnverifiedServerControl,
    serverIdentity: serverIdentity(env),
    allowOutOfRangeWrites,
    denyRead: file.deny_read ?? [],
    writableSubtrees: (control.writable_subtrees ?? []).map(subtreeRule),
    alarmSources: control.alarm_sources ?? [],
    alarmMaxSeverity: control.alarm_max_severity ?? null,
    preconditions: (control.preconditions ?? []).map((entry) => ({
      targets: entry.targets ?? [],
      methods: entry.methods ?? [],
      require: entry.require,
    })),
  };
}

/** Whether control may be offered over this connection, and why.
 *
 * The profile and allowlists apply on top of this; it only answers whether the
 * channel is one control may travel over at all. Each override covers exactly
 * one missing property, so neither can stand in for the other — and a secured
 * channel is judged on the server's identity even when
 * `OPCUA_ALLOW_INSECURE_CONTROL` is set, because that override was never about
 * identity.
 */
export function controlGate(config: PolicyConfig): ControlGate {
  const identity = config.serverIdentity;
  if (!identity.channelSecured) {
    return config.allowInsecureControl ? "INSECURE-OVERRIDE" : "blocked";
  }
  if (identity.serverAuthenticated) return "secured";
  return config.allowUnverifiedServerControl ? "UNVERIFIED-OVERRIDE" : "blocked";
}

/** The contract error a blocked gate refuses control with, or null if open.
 *
 * Two messages rather than one, because the fix differs: an unsecured channel
 * needs a security policy, an unverified server needs its certificate pinned.
 * Each names the variables that would open it.
 */
export function controlRefusal(config: PolicyConfig): string | null {
  if (controlGate(config) !== "blocked") return null;
  return config.serverIdentity.channelSecured
    ? "controlNeedsVerifiedServer"
    : "controlNeedsSecureChannel";
}

/** `serverStatus.server_identity`: the identity, and what it means for control. */
export interface ServerIdentityRecord {
  channel_secured: boolean;
  server_authenticated: boolean;
  authentication_method: "pin" | "none";
  control: ControlGate;
}

export function serverIdentityRecord(config: PolicyConfig): ServerIdentityRecord {
  const identity = config.serverIdentity;
  return {
    channel_secured: identity.channelSecured,
    server_authenticated: identity.serverAuthenticated,
    authentication_method: identity.authenticationMethod,
    control: controlGate(config),
  };
}

/** Whether a tool is offered at all, from its access class and the guard it declares.
 *
 * Exhaustive over `ACCESS_CLASSES` and closed by default. The previous version
 * ended in `return config.writableNodes.size > 0`, so an access class it did not
 * recognise — a typo, or a class added to the contract later — fell into the
 * *write* branch and became visible. Anything unrecognised is now denied, and
 * `full` does not exempt it: a profile that means "no allowlists" must not also
 * mean "no idea what this is, so yes".
 */
function classVisible(config: PolicyConfig, tool: ToolSpec): boolean {
  // The contract is data, so its `accessClass` can carry a typo the compiler
  // never sees. Checked against the list rather than trusted as the type.
  if (!(ACCESS_CLASSES as readonly string[]).includes(tool.accessClass)) return false;

  switch (tool.accessClass) {
    case "read":
      return true;
    case "monitor":
      // Deliberately not behind the secure-channel gate, and this is the place
      // to say why. That gate exists to stop *control* over a channel anyone can
      // read or forge. A subscription costs the server resources and delivers
      // values, but changes nothing in the plant — it is read, arriving by a
      // different route. Gating it would deny the profile's own default
      // (`observe`) its main tool on exactly the deployments that need to watch
      // something before they are allowed to touch it.
      return true;
    case "control":
    case "alarm-action":
      break;
    default:
      return false;
  }

  // A control tool must declare what needs authorising. Without that the policy
  // has nothing to check, and "nothing to check" must never read as "nothing to
  // stop it".
  const guard = tool.guard;
  if (!guard) return false;

  if (controlGate(config) === "blocked") return false;
  if (config.profile === "full") return true;
  if (config.profile !== "operator") return false;

  // Under `operator`, a tool is offered only if its allowlist could ever say
  // yes. Derived from the guard, so a new control tool needs no code here.
  if (guard.flag) return config[guard.flag];
  if (guard.methodPaths?.length) return config.callableMethods.size > 0;
  // Configured, not resolved: a subtree rule that matched nothing on this
  // session still makes the tool one the operator meant to offer, and the
  // catalogue must not move with the plant (#140).
  if (guard.nodeIdPaths?.length) {
    return config.writableNodes.size > 0 || config.writableSubtrees.length > 0;
  }
  return false;
}

/** Every value a guard path selects out of a call's arguments.
 *
 * Supports `field`, `array[].field`, and a trailing `array[]` for every string
 * element of a list of strings — which is how a read guard names `node_ids`. A
 * path that selects nothing yields nothing, and the caller treats that as a
 * denial rather than a pass: an argument the guard expected and did not find
 * means the call does not look like what the contract declared.
 */
export function valuesAt(args: Record<string, unknown>, path: string): string[] {
  const [head, ...rest] = path.split(".");
  if (head.endsWith("[]")) {
    const items = args[head.slice(0, -2)];
    if (!Array.isArray(items)) return [];
    if (rest.length === 0) {
      return items.filter((item): item is string => typeof item === "string");
    }
    const field = rest.join(".");
    return items.flatMap((item) =>
      item && typeof item === "object" ? valuesAt(item as Record<string, unknown>, field) : []
    );
  }
  if (rest.length > 0) {
    const nested = args[head];
    return nested && typeof nested === "object"
      ? valuesAt(nested as Record<string, unknown>, rest.join("."))
      : [];
  }
  const value = args[head];
  return typeof value === "string" ? [value] : [];
}

/** Every (node id, value) a `valuePaths` declaration selects, element-wise.
 *
 * Unlike `valuesAt`, which flattens because it only has to *collect* ids, this
 * has to keep each target with the value aimed at it: a batch write is a list of
 * independent (where, what) pairs and checking them crosswise would authorise a
 * value against the wrong node's bound.
 *
 * An element carrying no node id yields nothing. That is not a hole — the node
 * id is `required` by the contract schema and the identity allowlist has already
 * refused a write whose target cannot be located.
 */
export function pairsAt(
  args: Record<string, unknown>,
  spec: { array: string; nodeIdField: string; valueField: string }
): Array<[string, unknown]> {
  const items = args[spec.array];
  if (!Array.isArray(items)) return [];
  const found: Array<[string, unknown]> = [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const entry = item as Record<string, unknown>;
    const nodeId = entry[spec.nodeIdField];
    if (typeof nodeId === "string") found.push([nodeId, entry[spec.valueField]]);
  }
  return found;
}

/** `value` as a number for comparison, or null if it is not one.
 *
 * A string is parsed, because the write path accepts one for a numeric node and
 * parses it — `"42.5"` reaches the plant as 42.5, so a bound that did not look
 * inside the string would be trivially bypassed by quoting the number.
 *
 * A boolean is not a number here. `true` is not 1 to an operator writing a
 * bound, and letting it compare as one is the same coercion the typed-argument
 * work removed from method calls.
 *
 * The string is read with the one numeric grammar the write codec also uses
 * (`numeric.ts`), not `Number()`. `Number()` takes "0x10" as 16 while the
 * Python runtime's `float()` took "1_000", "inf" and "nan" instead (#157), so a
 * bound held on one runtime and not the other. A string the codec would not
 * write cannot be compared either.
 */
export function asNumber(value: unknown): number | null {
  if (typeof value === "boolean") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") return parseNumericString(value);
  return null;
}

/** A number as a refusal should read it: 100 rather than 100.0.
 *
 * `format_number` in `policy.py` is the other half, and the two must agree
 * character for character — the refusals they build are compared by
 * `tests/e2e/test_runtime_differential.py`.
 */
export function formatNumber(value: number): string {
  if (value === Infinity) return "infinity";
  if (value === -Infinity) return "-infinity";
  if (Number.isInteger(value) && Math.abs(value) < 1e16) return String(value);
  // Python's repr(float) and JavaScript's String(number) both produce the
  // shortest round-tripping decimal, so they agree on everything this can see.
  return String(value);
}

/** Whether a written value is one of an `enum` entry.
 *
 * Booleans and numbers are kept apart deliberately (`true` is not 1), and a
 * numeric string matches a numeric entry, because the write path parses it and
 * the plant sees the number.
 */
export function sameJsonValue(value: unknown, candidate: unknown): boolean {
  if (typeof value === "boolean" || typeof candidate === "boolean") return value === candidate;
  if (typeof candidate === "number") {
    const number = asNumber(value);
    return number !== null && number === candidate;
  }
  return value === candidate;
}

export class ToolPolicy {
  /** The server's NamespaceArray, once a session has reported it.
   *
   * `null` means "not yet known". That is not the same as "empty": an `nsu=`
   * allowlist entry cannot be resolved before the server has said what its
   * namespaces are, and resolving it wrongly would authorise a write to
   * whatever node happens to sit at that index. Unknown therefore denies.
   */
  private namespaces: readonly string[] | null = null;

  /** What each browse-path entry resolved to on this session; null for one that
   *  did not. Empty until the first resolution, and an entry that is not here
   *  matches nothing — the same reading as an `nsu=` entry before the namespace
   *  array is known. */
  private paths = new Map<string, string | null>();
  /** Every node writable_subtrees made writable on this session, with its rule's
   *  bound; the first rule to match a node wins. */
  private subtreeNodes = new Map<string, ValueBound>();
  /** Every node deny_read hides, as expanded on this session. The plain node-id
   *  entries are also checked directly, so they hide their node before the
   *  first expansion has run. */
  private deniedNodes = new Set<string>();
  /** Why the deny set is not known in full, or null when it is. While set, every
   *  guarded read is refused: a confidentiality rule that could not be applied in
   *  full must not be applied in part. */
  private denyIncomplete: string | null = null;

  constructor(readonly config: PolicyConfig) {}

  /** Bind what the browse-path entries resolved to, on every session.
   *
   * A path names a node by where it is, which is the one name that survives a
   * server that renumbers its nodes; it is resolved again on every session for
   * the same reason an `nsu=` entry is.
   */
  bindPaths(resolved: ReadonlyMap<string, string | null>): void {
    this.paths = new Map(resolved);
  }

  /** Bind the nodes writable_subtrees matched on this session. */
  bindSubtrees(nodes: ReadonlyMap<string, ValueBound>): void {
    this.subtreeNodes = new Map(nodes);
  }

  /** Bind the expanded deny set, and whether it is complete (`incomplete` null). */
  bindDenied(nodes: ReadonlySet<string>, incomplete: string | null): void {
    this.deniedNodes = new Set(nodes);
    this.denyIncomplete = incomplete;
  }

  /** Drop everything resolved against the old session, before resolving again.
   *
   * Fail closed in the meantime: path entries match nothing, subtree matches
   * write nothing, and a deny_read with entries refuses guarded reads until it
   * has been expanded on the new session — a node the old session's expansion
   * hid may sit somewhere else now.
   */
  forgetResolution(reason: string): void {
    this.paths = new Map();
    this.subtreeNodes = new Map();
    this.deniedNodes = new Set();
    this.denyIncomplete = this.config.denyRead.length > 0 ? reason : null;
  }

  /** The number of nodes the deny set holds, and whether it is complete. */
  denyState(): { size: number; complete: boolean } {
    return { size: this.deniedNodes.size, complete: this.denyIncomplete === null };
  }

  /** The canonical node id a policy entry names on this session, or null.
   *
   * Every entry goes through here — writable_nodes, callable_methods,
   * writable_subtrees roots, deny_read, alarm_sources, preconditions — so a
   * browse path works wherever a node id does. A request's own node id never
   * does: that goes through `resolve`, and a path there names nothing.
   */
  resolveEntry(entry: string): string | null {
    if (isBrowsePath(entry)) return this.paths.get(entry) ?? null;
    return this.resolve(entry);
  }

  /** A request's node id in the spelling this policy compares by, or null. */
  resolveNodeId(nodeId: string): string | null {
    return this.resolve(nodeId);
  }

  /** `nodeId` written against the namespace URI rather than its index, or null.
   *
   * For the audit record (spec §8): a namespace index means nothing once the
   * server that assigned it has restarted, and the URI is what lets a line be
   * read against the right node months later. Null when the index is not in the
   * NamespaceArray this session reported, or no array has been reported.
   */
  uriForm(nodeId: string): string | null {
    if (this.namespaces === null) return null;
    const resolved = this.resolve(nodeId);
    const match = resolved === null ? null : /^ns=([0-9]+);(.+)$/s.exec(resolved);
    if (!match) return null;
    const uri = this.namespaces[Number(match[1])];
    return uri === undefined ? null : `nsu=${uri};${match[2]}`;
  }

  /** Bind the live NamespaceArray, re-read on every (re)connect.
   *
   * Every connect, not just the first: a server that restarted may have loaded
   * its namespaces in a different order, and an allowlist pinned by URI has to
   * follow it there. That is the whole reason the `nsu=` form exists.
   */
  bindNamespaces(uris: readonly string[]): void {
    this.namespaces = [...uris];
    const unresolved = [...this.config.writableNodes].filter(
      (entry) => namespaceUriForm(entry) && resolveNodeId(entry, uris) === null
    );
    for (const entry of unresolved) {
      // Loud, because the failure it prevents is silent: the operator believes
      // a node is writable and every attempt is denied.
      console.error(
        `WARNING: policy entry "${entry}" names a namespace URI this server does not ` +
          `publish, so nothing can match it. Check the NamespaceArray with get_server_status.`
      );
    }
  }

  isVisible(tool: ToolSpec): boolean {
    return (
      classVisible(this.config, tool) &&
      (this.config.allowedTools === null || this.config.allowedTools.has(tool.name))
    );
  }

  visibleTools(tools: ToolSpec[]): ToolSpec[] {
    return tools.filter((tool) => this.isVisible(tool));
  }

  /** Authorize one call, or throw. The security boundary.
   *
   * Walks the tool's declared `guard` rather than switching on its name, so a
   * control tool added to the contract is checked by this code without it
   * changing — and is denied outright if it declares no guard.
   */
  authorize(name: string, args: Record<string, unknown> = {}): void {
    const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
    if (!tool) throw new Error(message("unknownTool", { tool: name }));
    if (!this.isVisible(tool)) throw new Error(this.refusal(tool));
    // Every profile: deny_read is a confidentiality control on reads, not an
    // operator allowlist, and `full` does not lift it.
    this.authorizeRead(name, args, false);
    if (this.config.profile !== "operator") return;

    // `isVisible` has already refused a guardless control tool; this is the
    // same refusal stated where the arguments are checked, so neither half can
    // be removed on the assumption that the other covers it.
    const guard = tool.guard;
    if (!guard) {
      if (tool.accessClass === "read" || tool.accessClass === "monitor") return;
      throw new Error(message("guardMissing", { tool: name }));
    }

    this.authorizeNodes(name, guard, args);
    this.authorizeValues(guard, args);
    this.authorizeMethods(name, guard, args);
  }

  /** Refuse a read of a node deny_read hides, under every profile (spec §6.3).
   *
   * Walks the tool's `readGuard`, as `authorize` walks its `guard`. Called twice
   * per call: from `authorize`, before anything is sent, with what is known so
   * far; and `strict` once the connection is up and the policy resolved on it,
   * when a deny set that could not be expanded in full refuses every guarded read
   * rather than letting through whatever it failed to name.
   */
  authorizeRead(name: string, args: Record<string, unknown>, strict: boolean): void {
    if (this.config.denyRead.length === 0) return;
    const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
    const paths = tool?.readGuard?.nodeIdPaths ?? [];
    if (paths.length === 0) return;
    if (strict && this.denyIncomplete !== null) {
      throw new ContractRefusal(message("readPolicyIncomplete", { reason: this.denyIncomplete }));
    }
    for (const path of paths) {
      for (const nodeId of valuesAt(args, path)) {
        if (this.isReadDenied(nodeId)) {
          throw new ContractRefusal(message("readDenied", { node_id: nodeId }));
        }
      }
    }
  }

  /** Whether deny_read hides `nodeId`. */
  isReadDenied(nodeId: string): boolean {
    if (this.config.denyRead.length === 0) return false;
    const wanted = this.resolve(nodeId);
    if (wanted === null) return false;
    if (this.deniedNodes.has(wanted)) return true;
    return this.config.denyRead.some(
      (entry) => !isBrowsePath(entry) && this.resolveEntry(entry) === wanted
    );
  }

  /** Why `name` is not callable at all, or null when it is offered. */
  toolRefusal(name: string): string | null {
    const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
    if (!tool) return message("unknownTool", { tool: name });
    return this.isVisible(tool) ? null : this.refusal(tool);
  }

  /** Whether `nodeId` is in the operator's writable set: writable_nodes, or a
   *  writable_subtrees match. Never throws. */
  isWritable(nodeId: string): boolean {
    const wanted = this.resolve(nodeId);
    if (wanted === null) return false;
    return this.writableSet().has(wanted);
  }

  /** Every node the operator may write, resolved, on this session. */
  writableSet(): Set<string> {
    const set = this.resolvedEntries(this.config.writableNodes);
    for (const nodeId of this.subtreeNodes.keys()) set.add(nodeId);
    return set;
  }

  /** Every (object, method) pair the operator may call, resolved, as `o|m`. */
  callableSet(): Set<string> {
    const allowed = new Set<string>();
    for (const entry of this.config.callableMethods) {
      const [entryObject, entryMethod] = entry.split("|");
      const resolvedObject = this.resolveEntry(entryObject);
      const resolvedMethod = this.resolveEntry(entryMethod);
      if (resolvedObject !== null && resolvedMethod !== null) {
        allowed.add(`${resolvedObject}|${resolvedMethod}`);
      }
    }
    return allowed;
  }

  /** The operator preconditions that guard a write to `nodeId` (spec §6.5). */
  preconditionsForNode(nodeId: string): Precondition[] {
    if (this.config.profile !== "operator") return [];
    const wanted = this.resolve(nodeId);
    if (wanted === null) return [];
    return this.config.preconditions.filter((precondition) =>
      precondition.targets.some((target) => this.resolveEntry(target) === wanted)
    );
  }

  /** The first precondition target or method pair that does not resolve, as
   *  written, or null when every one does (spec §12).
   *
   * An interlock whose target names nothing on this server guards nothing, and
   * the node it was meant for may still be writable through a plain id — so
   * under operator, while one does not resolve, no write and no method call is
   * sent at all. Policy order: each entry's targets, then its method pairs.
   */
  unresolvedPrecondition(): string | null {
    if (this.config.profile !== "operator") return null;
    for (const precondition of this.config.preconditions) {
      for (const target of precondition.targets) {
        if (this.resolveEntry(target) === null) return target;
      }
      for (const pair of precondition.methods) {
        if (
          this.resolveEntry(pair.object_id) === null ||
          this.resolveEntry(pair.method_id) === null
        ) {
          return `${pair.object_id}|${pair.method_id}`;
        }
      }
    }
    return null;
  }

  /** The operator preconditions that guard a call of `method` on `object`. */
  preconditionsForCall(object: string, method: string): Precondition[] {
    if (this.config.profile !== "operator") return [];
    const wantedObject = this.resolve(object);
    const wantedMethod = this.resolve(method);
    if (wantedObject === null || wantedMethod === null) return [];
    return this.config.preconditions.filter((precondition) =>
      precondition.methods.some(
        (pair) =>
          this.resolveEntry(pair.object_id) === wantedObject &&
          this.resolveEntry(pair.method_id) === wantedMethod
      )
    );
  }

  /** The operator's alarm scope, resolved, or null when it sets none (spec §6.4).
   *
   * `sources` is null when alarm_sources is not configured, and the resolved
   * entries when it is — an entry that did not resolve is dropped, so a list of
   * nothing but unresolved entries allows no source at all.
   */
  alarmScope(): { sources: string[] | null; maxSeverity: number | null } | null {
    const { profile, alarmSources, alarmMaxSeverity } = this.config;
    if (profile !== "operator") return null;
    if (alarmSources.length === 0 && alarmMaxSeverity === null) return null;
    return {
      sources: alarmSources.length === 0 ? null : [...this.resolvedEntries(alarmSources)],
      maxSeverity: alarmMaxSeverity,
    };
  }

  /** Why a hidden tool is hidden, worded so the reader knows what to change.
   *
   * The channel gate is named only where it is the reason: a control tool the
   * profile would otherwise offer and the allowlist does not exclude. Telling an
   * `observe` deployment to pin a certificate would be advice that changes
   * nothing.
   */
  private refusal(tool: ToolSpec): string {
    const { allowedTools, profile } = this.config;
    const refusal = controlRefusal(this.config);
    if (
      refusal !== null &&
      (tool.accessClass === "control" || tool.accessClass === "alarm-action") &&
      tool.guard &&
      (profile === "operator" || profile === "full") &&
      (allowedTools === null || allowedTools.has(tool.name))
    ) {
      return message(refusal, { tool: tool.name });
    }
    return message("toolDisabled", { tool: tool.name, profile });
  }

  /** Check every write in a call against the operator's bound for its target.
   *
   * And what is being written, not only where. Everything here is decidable
   * without touching the network, which is what keeps it in the authorization
   * layer: a refusal happens before a single byte is sent, so a batch can never
   * end up partially applied — the same property the identity allowlist had.
   */
  private authorizeValues(guard: ToolGuard, args: Record<string, unknown>): void {
    for (const spec of guard.valuePaths ?? []) {
      for (const [nodeId, value] of pairsAt(args, spec)) {
        const bound = this.boundFor(nodeId);
        if (!bound) continue;
        // An array write is checked element by element. Writing [0, 9999] to a
        // bounded node is writing 9999 to it.
        for (const element of Array.isArray(value) ? value : [value]) {
          this.checkOne(nodeId, element, bound);
        }
      }
    }
  }

  private checkOne(nodeId: string, value: unknown, bound: ValueBound): void {
    if (bound.allowed && !bound.allowed.some((candidate) => sameJsonValue(value, candidate))) {
      throw new Error(
        message("valueNotAllowed", {
          value: JSON.stringify(value) ?? String(value),
          node_id: nodeId,
          allowed: bound.allowed.map((item) => JSON.stringify(item)).join(", "),
        })
      );
    }
    if (bound.minimum === null && bound.maximum === null) return;
    const number = asNumber(value);
    if (number === null) {
      throw new Error(
        message("valueNotComparable", {
          node_id: nodeId,
          value: JSON.stringify(value) ?? String(value),
        })
      );
    }
    const low = bound.minimum ?? -Infinity;
    const high = bound.maximum ?? Infinity;
    if (number < low || number > high) {
      throw new Error(
        message("valueOutOfRange", {
          value: formatNumber(number),
          node_id: nodeId,
          low: formatNumber(low),
          high: formatNumber(high),
          unit: "",
          source: "the operator policy",
        })
      );
    }
  }

  /** The operator's value bound for `nodeId`, or null if it has none.
   *
   * Resolved rather than compared literally, for the same reason
   * `requireWritableNode` is: `nsu=…;i=5` and `ns=2;i=5` are the same node on a
   * server that publishes that URI at index 2, and a bound that only matched one
   * spelling would be a bound an operator believes is in force and is not.
   *
   * Public because the write path needs it too: `maxChange` is a bound on the
   * *move*, so it can only be checked against the node's current value, which is
   * a read and does not belong in the authorization layer.
   */
  boundFor(nodeId: string): ValueBound | null {
    const wanted = this.resolve(nodeId);
    if (wanted === null) return null;
    let explicit = false;
    for (const [entry, bound] of this.config.valueBounds) {
      if (this.resolveEntry(entry) !== wanted) continue;
      if (!isEmptyBound(bound)) return bound;
      explicit = true;
    }
    // A node an explicit writable_nodes entry names takes that entry's bound,
    // even none: the entry is the more specific statement. Subtree rules are
    // operator policy, and only bind there.
    if (explicit || this.config.profile !== "operator") return null;
    const bound = this.subtreeNodes.get(wanted);
    return bound && !isEmptyBound(bound) ? bound : null;
  }

  private authorizeNodes(name: string, guard: ToolGuard, args: Record<string, unknown>): void {
    for (const path of guard.nodeIdPaths ?? []) {
      const found = valuesAt(args, path);
      if (found.length === 0) {
        // The guard named an argument the call does not carry. Denying is the
        // only safe reading: a write whose target cannot be located is a write
        // whose target cannot be checked.
        throw new Error(`${name} requires ${path.replace("[]", "")} to authorize the write`);
      }
      for (const nodeId of found) {
        this.requireWritableNode(nodeId);
      }
    }
  }

  private authorizeMethods(name: string, guard: ToolGuard, args: Record<string, unknown>): void {
    for (const { objectPath, methodPath } of guard.methodPaths ?? []) {
      const [object] = valuesAt(args, objectPath);
      const [method] = valuesAt(args, methodPath);
      if (object === undefined || method === undefined) {
        throw new Error(`${name} requires ${objectPath} and ${methodPath} to authorize the call`);
      }
      const wantedObject = this.resolve(object);
      const wantedMethod = this.resolve(method);
      const allowed = this.callableSet();
      if (
        wantedObject === null ||
        wantedMethod === null ||
        !allowed.has(`${wantedObject}|${wantedMethod}`)
      ) {
        throw new Error(
          message("methodNotAllowed", { object_node_id: object, method_node_id: method })
        );
      }
    }
  }

  /** One node id in the spelling this policy compares by, or null if it has none.
   *
   * Null means "names a namespace this server does not publish". Callers must
   * treat that as a denial and must not substitute a placeholder: two *different*
   * unresolvable ids sharing one placeholder would compare equal, so an
   * allowlist entry for an unknown URI would authorise a request naming a
   * different unknown URI. The first draft did exactly that, and a test caught it.
   */
  private resolve(nodeId: string): string | null {
    return resolveNodeId(nodeId, this.namespaces ?? []);
  }

  /** Every allowlist entry that resolves, in comparable form.
   *
   * Entries that do not resolve are dropped rather than kept — an entry naming a
   * namespace this server does not publish can match nothing, and that is the
   * whole of what it should do.
   */
  private resolvedEntries(entries: Iterable<string>): Set<string> {
    const resolved = new Set<string>();
    for (const entry of entries) {
      const value = this.resolveEntry(entry);
      if (value !== null) resolved.add(value);
    }
    return resolved;
  }

  private requireWritableNode(nodeId: string): void {
    if (!this.isWritable(nodeId)) {
      throw new Error(message("nodeNotWritable", { node_id: nodeId }));
    }
  }
}

/** A policy read from the environment.
 *
 * A *factory*, not a singleton. This memoised one instance for the process,
 * which made two unrelated callers share a policy by accident rather than by
 * wiring — and the connection re-binds that policy's namespace mapping while the
 * tools authorize against it, so "the same object" is a correctness requirement
 * and not a convenience. `index.ts` now constructs one and passes it to both;
 * see #116 and the note there.
 *
 * Python's `tool_policy()` is the other half and is likewise uncached.
 */
export function toolPolicy(): ToolPolicy {
  return new ToolPolicy(parsePolicyConfig(process.env));
}

/** One-line summary for the startup log.
 *
 * Every state is named apart. The old version printed `insecure-control=enabled`
 * both for a properly secured deployment and for an active lab override, which
 * made the override the opposite of conspicuous — the one line an operator might
 * scan for it said the same thing either way. `server-identity` is here for the
 * same reason: `control=blocked` on a secured channel is otherwise a puzzle.
 */
export function describePolicy(policy: ToolPolicy): string {
  const { config } = policy;
  const identity = config.serverIdentity;
  const who = identity.serverAuthenticated
    ? identity.authenticationMethod
    : identity.channelSecured
      ? "unverified"
      : "none";
  return `profile=${config.profile} control=${controlGate(config)} server-identity=${who}`;
}
