import { readFileSync } from "fs";

import { CONTRACT, type ToolSpec } from "./contract.js";

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

function classVisible(config: PolicyConfig, tool: ToolSpec): boolean {
  const accessClass = tool.accessClass;
  if (accessClass === "read" || accessClass === "monitor") return true;
  if (!config.secureChannel && !config.allowInsecureControl) return false;
  if (config.profile === "full") return true;
  if (config.profile !== "operator") return false;
  if (accessClass === "alarm-action") return config.acknowledgeAlarms;
  if (tool.name === "call_opcua_method") return config.callableMethods.size > 0;
  return config.writableNodes.size > 0;
}

export class ToolPolicy {
  constructor(readonly config: PolicyConfig) {}

  isVisible(tool: ToolSpec): boolean {
    return (
      classVisible(this.config, tool) &&
      (this.config.allowedTools === null || this.config.allowedTools.has(tool.name))
    );
  }

  visibleTools(tools: ToolSpec[]): ToolSpec[] {
    return tools.filter((tool) => this.isVisible(tool));
  }

  authorize(name: string, args: Record<string, unknown> = {}): void {
    const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
    if (!tool) throw new Error(`Unknown tool: ${name}`);
    if (!this.isVisible(tool)) {
      throw new Error(`Tool "${name}" is disabled by OPCUA_PROFILE=${this.config.profile}`);
    }
    if (this.config.profile !== "operator") return;

    if (name === "write_opcua_node") {
      this.requireWritableNode(String(args.node_id ?? ""));
    } else if (name === "write_multiple_opcua_nodes") {
      if (!Array.isArray(args.nodes_to_write)) {
        throw new Error("write_multiple_opcua_nodes requires a nodes_to_write array");
      }
      const items = args.nodes_to_write;
      for (const item of items) {
        const nodeId = String((item as Record<string, unknown>)?.node_id ?? "");
        this.requireWritableNode(nodeId);
      }
    } else if (name === "call_opcua_method") {
      const key = `${String(args.object_node_id ?? "")}|${String(args.method_node_id ?? "")}`;
      if (!this.config.callableMethods.has(key)) {
        throw new Error(`Method ${key} is not allowed by the operator policy`);
      }
    }
  }

  private requireWritableNode(nodeId: string): void {
    if (!this.config.writableNodes.has(nodeId)) {
      throw new Error(`Node ${nodeId} is not writable under the operator policy`);
    }
  }
}

let cached: ToolPolicy | null = null;

export function toolPolicy(): ToolPolicy {
  if (!cached) cached = new ToolPolicy(parsePolicyConfig(process.env));
  return cached;
}

export function describePolicy(policy: ToolPolicy): string {
  const { config } = policy;
  const insecure = config.secureChannel || config.allowInsecureControl ? "enabled" : "blocked";
  return `profile=${config.profile} insecure-control=${insecure}`;
}
