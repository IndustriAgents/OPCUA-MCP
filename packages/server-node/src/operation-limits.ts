/** What the connected OPC UA server says one service call may carry (issue #139).
 *
 * `operation_limits.py` is the Python half. OPC UA Part 5 §6.3.11 has a server
 * publish its OperationLimits — how many nodes one Read, Write, Browse or
 * TranslateBrowsePathsToNodeIds may name — and a server is entitled to refuse a
 * request over them with BadTooManyOperations. Neither runtime asked, so a batch
 * that fitted this server's own caps could still be one the plant turned away
 * wholesale. The node ids are `contract/tools.json` -> `operationLimits`.
 *
 * Read once per session, beside the capability probes, and forgotten with the
 * session: a restarted server may be configured differently. What each tool
 * does with the answer — chunk a read, refuse a write — is the contract's
 * `operationLimits` comment, and `effectiveLimit` in `limits.ts` is how the
 * server's number is combined with ours.
 */

import { AttributeIds, ClientSession, StatusCodes } from "node-opcua-client";

import { CONTRACT } from "./contract.js";
import { MAX_NODES_PER_READ, MAX_NODES_PER_WRITE, effectiveLimit } from "./limits.js";

/** The server's stated limits, null where it states none. */
export interface ServerOperationLimits {
  maxNodesPerRead: number | null;
  maxNodesPerWrite: number | null;
  maxNodesPerBrowse: number | null;
  maxNodesPerTranslateBrowsePathsToNodeIds: number | null;
}

/** What is assumed before a session has been asked, and when asking fails. */
export const UNSTATED: ServerOperationLimits = {
  maxNodesPerRead: null,
  maxNodesPerWrite: null,
  maxNodesPerBrowse: null,
  maxNodesPerTranslateBrowsePathsToNodeIds: null,
};

const NAMES = Object.keys(UNSTATED) as Array<keyof ServerOperationLimits>;

/** Ask the server, in one Read of the four nodes.
 *
 * Best-effort, like the capability probes: the nodes are optional in Part 5, and
 * a server that does not publish one has stated no limit — which is `null`
 * here, and which leaves the project limit in force. A failure of the read
 * itself is the same answer, never a reason for a tool to fail.
 */
export async function readOperationLimits(session: ClientSession): Promise<ServerOperationLimits> {
  try {
    const values = await session.read(
      NAMES.map((name) => ({
        nodeId: CONTRACT.operationLimits[name],
        attributeId: AttributeIds.Value,
      }))
    );
    const limits = { ...UNSTATED };
    NAMES.forEach((name, index) => {
      const dataValue = values[index];
      const value = dataValue?.value?.value;
      if (dataValue?.statusCode === StatusCodes.Good && typeof value === "number") {
        limits[name] = value;
      }
    });
    return limits;
  } catch {
    return { ...UNSTATED };
  }
}

/** The per-request sizes in force, given what the server stated. */
export function readChunk(server: ServerOperationLimits, project = MAX_NODES_PER_READ): number {
  return effectiveLimit(project, server.maxNodesPerRead);
}

export function writeLimit(server: ServerOperationLimits): number {
  return effectiveLimit(MAX_NODES_PER_WRITE, server.maxNodesPerWrite);
}

export function browseChunk(server: ServerOperationLimits, project: number): number {
  return effectiveLimit(project, server.maxNodesPerBrowse);
}

export function translateChunk(server: ServerOperationLimits, project: number): number {
  return effectiveLimit(project, server.maxNodesPerTranslateBrowsePathsToNodeIds);
}
