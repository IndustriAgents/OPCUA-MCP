import { readFileSync } from "fs";

import { ACCESS_CLASSES } from "./access-classes.js";
import { CONTRACT, type ToolGuard, type ToolSpec } from "./contract.js";
import { namespaceUriForm, resolveNodeId } from "./node-ids.js";

export type ToolProfile = "observe" | "operator" | "full";

interface PolicyFile {
  version?: number;
  profile?: string;
  allowed_tools?: string[];
  allow_insecure_control?: boolean;
  control?: {
    writable_nodes?: string[];
    callable_methods?: Array<{ object_id: string; method_id: string }>;
    acknowledge_alarms?: boolean;
  };
}

export interface PolicyConfig {
  profile: ToolProfile;
  allowedTools: Set<string> | null;
  writableNodes: Set<string>;
  callableMethods: Set<string>;
  acknowledgeAlarms: boolean;
  allowInsecureControl: boolean;
  secureChannel: boolean;
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
  const writableNodes = new Set(
    parseCsv(value(env, "OPCUA_ALLOWED_WRITE_NODES")) ?? control.writable_nodes ?? []
  );

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
  const policy = value(env, "OPCUA_SECURITY_POLICY") ?? "None";

  return {
    profile,
    allowedTools,
    writableNodes,
    callableMethods,
    acknowledgeAlarms,
    allowInsecureControl,
    secureChannel: policy.toLowerCase() !== "none",
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
    if (!tool) throw new Error(`Unknown tool: ${name}`);
    if (!this.isVisible(tool)) {
      throw new Error(`Tool "${name}" is disabled by OPCUA_PROFILE=${this.config.profile}`);
    }
    if (this.config.profile !== "operator") return;

    // `isVisible` has already refused a guardless control tool; this is the
    // same refusal stated where the arguments are checked, so neither half can
    // be removed on the assumption that the other covers it.
    const guard = tool.guard;
    if (!guard) {
      if (tool.accessClass === "read" || tool.accessClass === "monitor") return;
      throw new Error(`Tool "${name}" declares no policy guard, so it cannot be authorized`);
    }

    this.authorizeNodes(name, guard, args);
    this.authorizeMethods(name, guard, args);
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
        throw new Error(`Method ${object}|${method} is not allowed by the operator policy`);
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
      throw new Error(`Node ${nodeId} is not writable under the operator policy`);
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
