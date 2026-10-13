/** Read use case. No SDK, session, native value or client-library dependency. */
import { ContractRefusal, message } from "../errors.js";
import { chunked } from "../limits.js";
import type { NodeFacts } from "../node-facts.js";
import { writeAccess, type WriteAccessPolicy, type WriteAccessRecord } from "../write-plan.js";

export interface ReadRecord {
  node_id: string;
  value: unknown;
  data_type: string | null;
  status: string;
  source_timestamp: string | null;
  server_timestamp: string | null;
  engineering: unknown;
  write_access?: WriteAccessRecord | null;
}

/** Bound to one session by the adapter; results have already crossed its codec. */
export interface ReadPort {
  values(nodeIds: string[]): Promise<ReadRecord[]>;
  engineering(nodeIds: string[]): Promise<Map<string, unknown>>;
  /** What each node says about itself; null where nothing could be read. Never throws. */
  facts(nodeIds: string[]): Promise<Map<string, NodeFacts | null>>;
}

/** What `write_access` is told about the policy for one node. */
export type WriteAccessPolicyFor = (nodeId: string) => WriteAccessPolicy;

type AccessEngineering = Parameters<typeof writeAccess>[0]["engineering"];

/** `read_opcua_nodes`. With `writeAccessFor`, every record also says whether a
 *  write would go through and what it must look like (`write_access`); without
 *  it the field is left as the adapter set it, null. */
export async function readNodes(
  port: ReadPort,
  nodeIds: string[],
  chunk: number,
  writeAccessFor: WriteAccessPolicyFor | null = null
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
  // Only when asked: it costs a read of eight attributes per node the first time
  // a node is asked about in a session, and most reads are not about writing.
  // The same facts and the same policy answers the write path decides on, so
  // what is reported is what a write would meet — short of the OPC UA server's
  // own verdict, which only trying can give.
  const facts = writeAccessFor === null ? null : await port.facts(nodeIds);
  return records.map((record, index) => {
    const nodeId = nodeIds[index];
    const info = engineering.get(nodeId) ?? null;
    const read = { ...record, engineering: info };
    if (facts === null || writeAccessFor === null) return read;
    return {
      ...read,
      write_access: writeAccess({
        node_id: nodeId,
        facts: facts.get(nodeId) ?? null,
        engineering: info as AccessEngineering,
        policy: writeAccessFor(nodeId),
      }),
    };
  });
}
