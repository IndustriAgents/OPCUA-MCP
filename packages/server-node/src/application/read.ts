/** Read use case. No SDK, session, native value or client-library dependency. */
import { ContractRefusal, message } from "../errors.js";
import { chunked } from "../limits.js";

export interface ReadRecord {
  node_id: string;
  value: unknown;
  data_type: string | null;
  status: string;
  source_timestamp: string | null;
  server_timestamp: string | null;
  engineering: unknown;
}

/** Bound to one session by the adapter; results have already crossed its codec. */
export interface ReadPort {
  values(nodeIds: string[]): Promise<ReadRecord[]>;
  engineering(nodeIds: string[]): Promise<Map<string, unknown>>;
}

export async function readNodes(
  port: ReadPort,
  nodeIds: string[],
  chunk: number
): Promise<ReadRecord[]> {
  if (nodeIds.length === 0) {
    throw new ContractRefusal(
      message("emptyArray", { tool: "read_opcua_nodes", argument: "node_ids" })
    );
  }
  const records: ReadRecord[] = [];
  for (const part of chunked(nodeIds, chunk)) records.push(...(await port.values(part)));
  // Metadata is fetched for the logical read, rather than once per service chunk.
  const engineering = await port.engineering(nodeIds);
  return records.map((record, index) => ({
    ...record,
    engineering: engineering.get(nodeIds[index]) ?? null,
  }));
}
