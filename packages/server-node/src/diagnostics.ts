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
export async function readServerStatus(
  session: ClientSession,
  endpointUrl: string,
  security: string
): Promise<ServerStatusRecord> {
  const [statusValue, namespaceValue] = await session.readVariableValue([
    CONTRACT.diagnostics.serverStatusNodeId,
    CONTRACT.diagnostics.namespaceArrayNodeId,
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
    namespaces,
    error: null,
  };
}
