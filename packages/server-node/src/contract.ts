// The shared tool contract, the configuration schema and the package version, all
// staged into build/ by scripts/prepare-build.mjs so the published package is
// self-contained.
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

export const BUILD_DIR = dirname(fileURLToPath(import.meta.url));

export type { AccessClass } from "./access-classes.js";
import type { AccessClass } from "./access-classes.js";

/** Where a control tool keeps the identifiers the policy layer must authorise.
 *
 * Declared in the contract beside the tool, so the policy walks a declaration
 * rather than switching on tool *names* — which is what let a newly added
 * control tool become callable with no argument checking at all. A `control` or
 * `alarm-action` tool with no `guard` is denied outright.
 */
export interface ToolGuard {
  /** Argument paths holding node ids to check against the writable allowlist.
   *  `a[].b` means "field b of every element of array a". */
  nodeIdPaths?: string[];
  /** Argument path pairs naming an (object, method) call to check. */
  methodPaths?: Array<{ objectPath: string; methodPath: string }>;
  /** A policy flag that must be true; the whole tool is gated on it. */
  flag?: "acknowledgeAlarms";
  /** Arrays whose elements pair a target with the value aimed at it, so the
   *  policy can check both together. Node identity is not the whole of a write:
   *  an allowlisted setpoint that accepts any number is authorised for 9999 as
   *  readily as for 99.9. */
  valuePaths?: Array<{ array: string; nodeIdField: string; valueField: string }>;
  /** Extra argument paths the audit record should carry, for a tool whose
   *  targets are not node ids. */
  auditPaths?: string[];
}

export interface ToolSpec {
  name: string;
  accessClass: AccessClass;
  /** Capabilities of which the server must report at least one; empty means always. */
  capabilities: string[];
  description: string;
  inputSchema: any;
  resultShape?: string;
  guard?: ToolGuard;
  annotations: {
    readOnlyHint: boolean;
    destructiveHint: boolean;
    idempotentHint: boolean;
  };
  /** What this server may do when the OPC UA session dies underneath the request.
   *
   * Deliberately not `annotations.idempotentHint`, which this runtime used to
   * read for it: that annotation is advice to the *model* about calling a tool
   * twice, and this decides whether the *transport* may put a second request on
   * the wire after an uncertain outcome. See `retryPolicies` in the contract.
   */
  retryPolicy: "resend" | "reconnectOnly" | "uncertainOutcome";
}

export const CONTRACT: {
  resultShapes: Record<string, any>;
  capabilities: Record<string, { nodeId: string; browseName: string; check: string }>;
  /** Prose for each `ToolSpec.retryPolicy` value; the tools name one of its keys. */
  retryPolicies: Record<string, string>;
  /** What tells a failure of the connection from a failure of the request;
   *  see connection.ts. */
  deadSession: {
    statusCodeNames: { names: string[] };
    socketErrors: { codes: string[] };
    phrases: { texts: string[] };
  };
  /** Where a node says what its number means; see node-metadata.ts. */
  analog: {
    engineeringUnitsBrowseName: string;
    euRangeBrowseName: string;
    instrumentRangeBrowseName: string;
    enforceEuRangeOnWrite: boolean;
    maxPropertiesPerRequest: number;
  };
  diagnostics: {
    serverStatusNodeId: string;
    namespaceArrayNodeId: string;
    serverDiagnosticsSummaryNodeId: string;
    diagnosticsFields: string[];
  };
  subscriptions: {
    defaultPublishingIntervalMs: number;
    minPublishingIntervalMs: number;
    defaultBufferSize: number;
    minBufferSize: number;
    maxBufferSize: number;
    /** Prose for each deadband kind and trigger; see subscriptions.ts. */
    deadbandTypes: Record<string, string>;
    dataChangeTriggers: Record<string, string>;
    defaultDataChangeTrigger: string;
  };
  traversal: {
    rootNodeId: string;
    defaultDepth: number;
    maxDepth: number;
    defaultMaxNodes: number;
    maxNodes: number;
    skipBrowseName: string;
    hasTypeDefinitionNodeId: string;
    maxTypeDefinitionsPerRequest: number;
  };
  events: {
    defaultNotifierNodeId: string;
    baseEventTypeNodeId: string;
    conditionTypeNodeId: string;
    conditionRefreshMethodNodeId: string;
    acknowledgeMethodNodeId: string;
    /** What each alarm action calls, and what it takes; see events.ts. */
    shelvingStateBrowseName: string;
    actions: Record<
      string,
      { browseName: string; methodNodeId: string; on: string; takes: string }
    >;
    refreshStartEventTypeNodeId: string;
    refreshEndEventTypeNodeId: string;
    defaults: {
      severityMin: number;
      bufferSize: number;
      readLimit: number;
      refreshTimeoutSeconds: number;
    };
    fields: Array<{ key: string; path: string }>;
  };
  /** Message templates for every failure a tool call can return; see errors.ts. */
  errors: Record<string, string>;
  /** Message templates for notices added beside a result; see notices.ts. */
  notices: Record<string, string>;
  /** How much one call may ask for. Refusals, not tuning knobs. */
  limits: { maxNodesPerRead: number; maxHistoryValues: number; maxSubscriptions: number };
  /** What the OPC UA server on the other end may send us; see transport-limits.ts. */
  transport: { maxChunkCount: number; maxChunkSize: number; maxMessageSize: number };
  resources: Array<{
    uri: string;
    name: string;
    description: string;
    mimeType: string;
    body: { recordsKey: string; resultShape: string };
  }>;
  tools: ToolSpec[];
} = JSON.parse(readFileSync(join(BUILD_DIR, "contract.json"), "utf8"));

// Version is single-sourced from package.json and staged into build/version.json
// by scripts/prepare-build.mjs, so it can never drift from what npm publishes.
export const { version: VERSION }: { version: string } = JSON.parse(
  readFileSync(join(BUILD_DIR, "version.json"), "utf8")
);

/** One environment variable, as `/contract/config.json` declares it. See that
 *  file's `fields` for what each member means. */
export interface ConfigSetting {
  env: string;
  key: string;
  category: string;
  title: string;
  description: string;
  type: "string" | "enum" | "boolean" | "number" | "path" | "list";
  choices?: string[];
  choiceAliases?: Record<string, string>;
  runtimeChoices?: Partial<Record<"node" | "python", string[]>>;
  minimum?: number;
  maximum?: number;
  integer?: boolean;
  mustExist?: boolean;
  contents?: string;
  itemFormat?: string;
  default: string | number | boolean | null;
  defaultDescription?: string;
  example?: string;
  required: boolean;
  secret: boolean;
  sensitive: boolean;
  securityRelevant: boolean;
  securityNote?: string;
  runtimes: Array<"node" | "python">;
  surfaces: Array<"mcpb" | "registry" | "installer" | "docs">;
  deprecatedAliases?: Array<{ env: string; removeIn: string }>;
}

export interface ConfigSchema {
  version: number;
  categories: Record<string, { title: string; description: string }>;
  booleanValues: { true: string[]; false: string[] };
  settings: ConfigSetting[];
}

let configSchemaCache: ConfigSchema | null = null;

/** Every setting this server reads, from the canonical schema (#133).
 *
 * Read on first use rather than at import: the server's own startup does not
 * need it, so a fault in it must not be able to stop the server starting. It
 * exists for code that runs from an installed package and has to describe the
 * configuration surface — `--install` (#135) — without a second hand-kept list.
 */
export function configSchema(): ConfigSchema {
  configSchemaCache ??= JSON.parse(readFileSync(join(BUILD_DIR, "config.json"), "utf8"));
  return configSchemaCache as ConfigSchema;
}
