/** Status orchestration over a native-free diagnostics port. */
import type { ServerIdentityRecord } from "../policy.js";
import { describeError, message } from "../errors.js";
/** What the OPC UA server says it is (`serverStatus.build_info`). */
export interface BuildInfoRecord {
  product_name: string;
  product_uri: string;
  manufacturer_name: string;
  software_version: string;
  build_number: string;
  build_date: string | null;
}

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
  server_identity: ServerIdentityRecord;
  server_state: string | null;
  current_time: string | null;
  start_time: string | null;
  build_info: BuildInfoRecord | null;
  diagnostics: DiagnosticsRecord | null;
  namespaces: NamespaceRecord[];
  error: string | null;
}

/** The report for a connection that is not up: configuration, and why.
 *
 * `server_identity` is reported here too: it comes from configuration, and "why
 * are the control tools missing?" is as likely a question while the connection
 * is down as while it is up.
 */
export function disconnectedStatus(
  endpointUrl: string,
  security: string,
  serverIdentity: ServerIdentityRecord,
  error: string | null
): ServerStatusRecord {
  return {
    connected: false,
    endpoint_url: endpointUrl,
    security,
    server_identity: serverIdentity,
    server_state: null,
    current_time: null,
    start_time: null,
    build_info: null,
    diagnostics: null,
    namespaces: [],
    error,
  };
}

export interface ConnectionSnapshot {
  endpoint: string;
  connecting: boolean;
  lastError: string | null;
}
export interface DiagnosticsPort<C> {
  snapshot(): ConnectionSnapshot;
  read(security: string, identity: ServerIdentityRecord): Promise<ServerStatusRecord>;
  capabilities(): C;
}
export async function getServerStatus<C>(
  port: DiagnosticsPort<C>,
  security: string,
  identity: ServerIdentityRecord
): Promise<ServerStatusRecord & { capabilities: C }> {
  const snapshot = port.snapshot();
  let status: ServerStatusRecord;
  if (snapshot.connecting)
    status = disconnectedStatus(
      snapshot.endpoint,
      security,
      identity,
      message("stillConnecting", {
        url: snapshot.endpoint,
        reason: snapshot.lastError ?? "not yet known",
      })
    );
  else {
    try {
      status = await port.read(security, identity);
    } catch (error) {
      status = disconnectedStatus(snapshot.endpoint, security, identity, describeError(error));
    }
  }
  return { ...status, capabilities: port.capabilities() };
}
