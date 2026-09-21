import { readFileSync } from "fs";

import { ACCESS_CLASSES } from "./access-classes.js";
import { CONTRACT, type ToolGuard, type ToolSpec } from "./contract.js";
import { namespaceUriForm, resolveNodeId } from "./node-ids.js";
import { message } from "./errors.js";

export type ToolProfile = "observe" | "operator" | "full";

/** One `writable_nodes` entry when it carries a bound rather than only a node. */
interface WritableNodeEntry {
  node: string;
  min?: number;
  max?: number;
  enum?: unknown[];
  max_change?: number;
}

interface PolicyFile {
  version?: number;
  profile?: string;
  allowed_tools?: string[];
  allow_insecure_control?: boolean;
  allow_out_of_range_writes?: boolean;
  control?: {
    writable_nodes?: Array<string | WritableNodeEntry>;
    callable_methods?: Array<{ object_id: string; method_id: string }>;
    acknowledge_alarms?: boolean;
  };
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

function boundNumber(entry: WritableNodeEntry, key: "min" | "max" | "max_change"): number | null {
  const value = entry[key];
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`writable_nodes entry "${entry.node}" has a non-numeric ${key}`);
  }
  return value;
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
  const unknown = Object.keys(entry).filter((key) => !BOUND_KEYS.includes(key));
  if (unknown.length > 0) {
    // Loud, because the failure it prevents is silent: an operator who writes
    // "minimum" instead of "min" believes a bound is in force and none is.
    throw new Error(
      `Unknown key in a writable_nodes entry: ${unknown.sort()[0]}. ` +
        `Use one of: ${[...BOUND_KEYS].sort().join(", ")}`
    );
  }
  if (typeof entry.node !== "string" || entry.node.trim() === "") {
    throw new Error("A writable_nodes entry must name a node");
  }
  if (entry.enum !== undefined && (!Array.isArray(entry.enum) || entry.enum.length === 0)) {
    throw new Error(`writable_nodes entry "${entry.node}" has an empty or non-list enum`);
  }
  const bound: ValueBound = {
    minimum: boundNumber(entry, "min"),
    maximum: boundNumber(entry, "max"),
    allowed: entry.enum ?? null,
    maxChange: boundNumber(entry, "max_change"),
  };
  if (bound.minimum !== null && bound.maximum !== null && bound.minimum > bound.maximum) {
    throw new Error(`writable_nodes entry "${entry.node}" has min above max`);
  }
  if (bound.maxChange !== null && bound.maxChange < 0) {
    throw new Error(`writable_nodes entry "${entry.node}" has a negative max_change`);
  }
  return [entry.node, bound];
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
  allowInsecureControl: boolean;
  secureChannel: boolean;
  /** Whether a write outside the range the OPC UA server itself published
   *  (`EURange`) is allowed through. Refused by default: a bound the equipment
   *  declares is worth more than one a human retyped, and it is the only value
   *  bound that exists on a deployment with no policy file at all. */
  allowOutOfRangeWrites: boolean;
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
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`OPCUA_POLICY_FILE ${path} must contain a JSON object`);
  }
  const file = parsed as PolicyFile;
  if (file.version !== undefined && file.version !== 1) {
    throw new Error(`Unsupported OPC UA policy version ${file.version}; expected 1`);
  }
  return file;
}

function validateToolNames(names: string[] | undefined): Set<string> | null {
  if (names === undefined) return null;
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
  const control = file.control || {};
  const profile = parseProfile(value(env, "OPCUA_PROFILE") ?? file.profile);
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
  const allowOutOfRangeWrites = parseBoolean(
    value(env, "OPCUA_ALLOW_OUT_OF_RANGE_WRITES"),
    "OPCUA_ALLOW_OUT_OF_RANGE_WRITES",
    file.allow_out_of_range_writes ?? false
  );
  const policy = value(env, "OPCUA_SECURITY_POLICY") ?? "None";

  return {
    profile,
    allowedTools,
    writableNodes,
    valueBounds,
    callableMethods,
    acknowledgeAlarms,
    allowInsecureControl,
    secureChannel: policy.toLowerCase() !== "none",
    allowOutOfRangeWrites,
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

  if (!config.secureChannel && !config.allowInsecureControl) return false;
  if (config.profile === "full") return true;
  if (config.profile !== "operator") return false;

  // Under `operator`, a tool is offered only if its allowlist could ever say
  // yes. Derived from the guard, so a new control tool needs no code here.
  if (guard.flag) return config[guard.flag];
  if (guard.methodPaths?.length) return config.callableMethods.size > 0;
  if (guard.nodeIdPaths?.length) return config.writableNodes.size > 0;
  return false;
}

/** Every value a guard path selects out of a call's arguments.
 *
 * Supports `field` and `array[].field`. A path that selects nothing yields
 * nothing, and the caller treats that as a denial rather than a pass: an
 * argument the guard expected and did not find means the call does not look
 * like what the contract declared.
 */
export function valuesAt(args: Record<string, unknown>, path: string): string[] {
  const [head, ...rest] = path.split(".");
  if (head.endsWith("[]")) {
    const items = args[head.slice(0, -2)];
    if (!Array.isArray(items)) return [];
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
 */
export function asNumber(value: unknown): number | null {
  if (typeof value === "boolean") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }
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
function sameJsonValue(value: unknown, candidate: unknown): boolean {
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

  constructor(readonly config: PolicyConfig) {}

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
    if (!this.isVisible(tool)) {
      throw new Error(message("toolDisabled", { tool: name, profile: this.config.profile }));
    }
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
    for (const [entry, bound] of this.config.valueBounds) {
      if (!isEmptyBound(bound) && this.resolve(entry) === wanted) return bound;
    }
    return null;
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
      const allowed = new Set<string>();
      for (const entry of this.config.callableMethods) {
        const [entryObject, entryMethod] = entry.split("|");
        const resolvedObject = this.resolve(entryObject);
        const resolvedMethod = this.resolve(entryMethod);
        if (resolvedObject !== null && resolvedMethod !== null) {
          allowed.add(`${resolvedObject}|${resolvedMethod}`);
        }
      }
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
  private resolvedSet(entries: Iterable<string>): Set<string> {
    const resolved = new Set<string>();
    for (const entry of entries) {
      const value = this.resolve(entry);
      if (value !== null) resolved.add(value);
    }
    return resolved;
  }

  private requireWritableNode(nodeId: string): void {
    const wanted = this.resolve(nodeId);
    if (wanted === null || !this.resolvedSet(this.config.writableNodes).has(wanted)) {
      throw new Error(message("nodeNotWritable", { node_id: nodeId }));
    }
  }
}

let cached: ToolPolicy | null = null;

export function toolPolicy(): ToolPolicy {
  if (!cached) cached = new ToolPolicy(parsePolicyConfig(process.env));
  return cached;
}

/** One-line summary for the startup log.
 *
 * The three states are named apart. The old version printed
 * `insecure-control=enabled` both for a properly secured deployment and for an
 * active lab override, which made the override the opposite of conspicuous —
 * the one line an operator might scan for it said the same thing either way.
 */
export function describePolicy(policy: ToolPolicy): string {
  const { config } = policy;
  const control = config.secureChannel
    ? "secured"
    : config.allowInsecureControl
      ? "INSECURE-OVERRIDE"
      : "blocked";
  return `profile=${config.profile} control=${control}`;
}
