// The shared tool contract and the package version, both staged into build/ by
// scripts/prepare-build.mjs so the published package is self-contained.
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

export const BUILD_DIR = dirname(fileURLToPath(import.meta.url));

/** Every access class the contract may declare.
 *
 * A list rather than only a type, because the policy layer needs to recognise
 * an *unknown* class at runtime — a contract is data, and data can carry a typo
 * that the compiler never sees. `ACCESS_CLASSES` in `policy.py` is the Python
 * half of this.
 */
export const ACCESS_CLASSES = ["read", "monitor", "alarm-action", "control"] as const;

export type AccessClass = (typeof ACCESS_CLASSES)[number];

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
}

export interface ToolSpec {
  name: string;
  accessClass: AccessClass;
  capability: string | null;
  description: string;
  inputSchema: any;
  resultShape?: string;
  guard?: ToolGuard;
  annotations: {
    readOnlyHint: boolean;
    destructiveHint: boolean;
    idempotentHint: boolean;
  };
}

export const CONTRACT: {
  resultShapes: Record<string, any>;
  capabilities: Record<string, { nodeId: string; browseName: string; check: string }>;
  diagnostics: { serverStatusNodeId: string; namespaceArrayNodeId: string };
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
