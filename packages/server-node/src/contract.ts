// The shared tool contract and the package version, both staged into build/ by
// scripts/prepare-build.mjs so the published package is self-contained.
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
  diagnostics: { serverStatusNodeId: string; namespaceArrayNodeId: string };
  subscriptions: {
    defaultPublishingIntervalMs: number;
    minPublishingIntervalMs: number;
    defaultBufferSize: number;
    minBufferSize: number;
    maxBufferSize: number;
  };
  traversal: {
    rootNodeId: string;
    defaultDepth: number;
    maxDepth: number;
    defaultMaxNodes: number;
    maxNodes: number;
    skipBrowseName: string;
  };
  events: {
    defaultNotifierNodeId: string;
    baseEventTypeNodeId: string;
    conditionTypeNodeId: string;
    conditionRefreshMethodNodeId: string;
    acknowledgeMethodNodeId: string;
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
