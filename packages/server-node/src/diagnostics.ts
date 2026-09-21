// The health/diagnostics report behind `get_server_status`.
//
// `contract/tools.json` -> `resultShapes.serverStatus` is the specification;
// this module is the Node implementation of it and `diagnostics.py` is the
// Python one. Both must produce the same record against the same OPC UA server:
// an agent that has learned one runtime's answer to "are we connected, to what,
// and is it healthy?" has to be able to read the other's.
import { ClientSession, ServerState } from "node-opcua-client";

import { CONTRACT } from "./contract.js";

/** What the OPC UA server says it is (`serverStatus.build_info`). */
export interface BuildInfoRecord {
  product_name: string;
  product_uri: string;
  manufacturer_name: string;
  software_version: string;
  build_number: string;
  build_date: string | null;
}

/** The record's field order, from the contract.
 *
 * `diagnostics.py` builds the same keys in the same order — twelve counters
 * reported in two different orders by two servers would be two records, not one
 * shape.
 */
export const DIAGNOSTICS_FIELDS: readonly string[] = CONTRACT.diagnostics.diagnosticsFields;

/** node-opcua decodes ServerDiagnosticsSummaryDataType with camelCase field
 *  names; the record uses snake_case. Derived rather than written out, so the
 *  two can only disagree if the spec's own spelling changes. */
const SUMMARY_PROPERTIES: Record<string, string> = Object.fromEntries(
  DIAGNOSTICS_FIELDS.map((field) => {
    const [head, ...rest] = field.split("_");
    return [field, head + rest.map((part) => part[0].toUpperCase() + part.slice(1)).join("")];
  })
);

/** The server's own ServerDiagnosticsSummary (`serverStatus.diagnostics`). */
export type DiagnosticsRecord = Record<string, number>;

/** One namespace of the server's NamespaceArray. */
export interface NamespaceRecord {
  index: number;
  uri: string;
}

/** The record `get_server_status` returns (`resultShapes.serverStatus`). */
export interface ServerStatusRecord {
  connected: boolean;
  endpoint_url: string;
  security: string;
  server_state: string | null;
  current_time: string | null;
  start_time: string | null;
  build_info: BuildInfoRecord | null;
  diagnostics: DiagnosticsRecord | null;
  namespaces: NamespaceRecord[];
  error: string | null;
}

/** The report for a connection that is not up: configuration, and why. */
export function disconnectedStatus(
  endpointUrl: string,
  security: string,
  error: string | null
): ServerStatusRecord {
  return {
    connected: false,
    endpoint_url: endpointUrl,
    security,
    server_state: null,
    current_time: null,
    start_time: null,
    build_info: null,
    diagnostics: null,
    namespaces: [],
    error,
  };
}

/** A DateTime as ISO-8601 UTC, mirroring the Python server's `format_iso_utc`. */
function toIsoUtc(value: unknown): string | null {
  if (!(value instanceof Date) || Number.isNaN(value.getTime())) return null;
  return value.toISOString();
}

/** A field the server may have left unset, as the string the contract promises. */
function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  // A LocalizedText (ProductName, ManufacturerName) carries its text under
  // `.text`; python-opcua hands back the bare string, so unwrap to match.
  const localized = (value as { text?: unknown }).text;
  if (typeof localized === "string") return localized;
  return typeof value === "string" ? value : String(value);
}

/** OPC UA's own name for a ServerState, e.g. 'Running'.
 *
 * node-opcua decodes the field as its numeric enum value, python-opcua as a
 * `ua.ServerState`; both runtimes report the *name*, so the answer does not
 * depend on which client library read it.
 */
function stateName(value: unknown): string | null {
  if (typeof value !== "number") return null;
  const name = (ServerState as Record<number, string>)[value];
  return typeof name === "string" ? name : "Unknown";
}

function buildInfo(raw: any): BuildInfoRecord | null {
  if (!raw) return null;
  return {
    product_name: text(raw.productName),
    product_uri: text(raw.productUri),
    manufacturer_name: text(raw.manufacturerName),
    software_version: text(raw.softwareVersion),
    build_number: text(raw.buildNumber),
    build_date: toIsoUtc(raw.buildDate),
  };
}

/** Read ServerStatus and the NamespaceArray over a live session.
 *
 * Both are mandatory nodes in OPC UA Part 5, so no browsing is needed to find
 * them — the node IDs come from the shared contract, which is also where the
 * Python server gets them.
 */
/** The server's own ServerDiagnosticsSummary, or null if it publishes none.
 *
 * Best-effort by design, and `null` is a real answer rather than a failure: Part
 * 5 lets a server leave diagnostics switched off, and the bundled Python mock
 * creates the node but never populates it. A server that cannot say how many
 * sessions it is holding is still a server worth talking to, so this must never
 * be the reason `get_server_status` fails — which is the one tool that has to
 * answer when everything else is going wrong.
 */
function diagnosticsSummary(summary: unknown): DiagnosticsRecord | null {
  if (!summary || typeof summary !== "object") return null;
  const source = summary as Record<string, unknown>;
  const record: DiagnosticsRecord = {};
  for (const [field, property] of Object.entries(SUMMARY_PROPERTIES)) {
    const value = source[property];
    if (typeof value !== "number") {
      // A structure that decoded but is missing a counter is not a summary this
      // server can report honestly, and a record with holes in it is worse than
      // no record.
      return null;
    }
    record[field] = value;
  }
  return record;
}

export async function readServerStatus(
  session: ClientSession,
  endpointUrl: string,
  security: string
): Promise<ServerStatusRecord> {
  const [statusValue, namespaceValue, diagnosticsValue] = await session.readVariableValue([
    CONTRACT.diagnostics.serverStatusNodeId,
    CONTRACT.diagnostics.namespaceArrayNodeId,
    // In the same batch: it is one more node on a read that was already
    // happening, so reporting it costs no extra round trip.
    CONTRACT.diagnostics.serverDiagnosticsSummaryNodeId,
  ]);

  const status: any = statusValue?.value?.value ?? null;
  const uris: unknown = namespaceValue?.value?.value ?? null;
  const namespaces: NamespaceRecord[] = Array.isArray(uris)
    ? uris.map((uri, index) => ({ index, uri: text(uri) }))
    : [];

  return {
    connected: true,
    endpoint_url: endpointUrl,
    security,
    server_state: status ? stateName(status.state) : null,
    current_time: status ? toIsoUtc(status.currentTime) : null,
    start_time: status ? toIsoUtc(status.startTime) : null,
    build_info: buildInfo(status?.buildInfo),
    diagnostics: diagnosticsSummary(diagnosticsValue?.value?.value),
    namespaces,
    error: null,
  };
}
