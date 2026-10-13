/** Address-space traversal over normalized references; no MCP or native SDK. */
import { CONTRACT } from "../contract.js";
import { canonicalNodeId } from "../node-ids.js";
import { ContractRefusal, describeError, message } from "../errors.js";
import { traversalCompleteness } from "../completeness.js";

export interface NodeRefRecord {
  node_id: string;
  browse_name: string;
  node_class: string;
  parent_node_id: string;
  data_type: string | null;
  value: unknown;
  description: string | null;
  type_definition: string | null;
}
export interface BrowseReference {
  nodeId: string;
  namespaceIndex: number;
  name: string | null;
  nodeClass: string;
}
export interface BrowsePort {
  children(nodeId: string): Promise<BrowseReference[]>;
  describe(nodeId: string, parentNodeId: string): Promise<NodeRefRecord>;
  enrich(records: NodeRefRecord[], includeValues: boolean): Promise<void>;
}
export interface BrowseRequest {
  nodeId?: string;
  browsePath?: string;
  depth?: number;
  nodeClass?: string;
  nameFilter?: string;
  includeValues?: boolean;
  maxNodes?: number;
}
export function browseNameMatches(
  segment: string,
  namespaceIndex: number,
  name: string | null
): boolean {
  const separator = segment.indexOf(":");
  if (separator > 0) {
    const index = Number(segment.slice(0, separator));
    if (Number.isInteger(index))
      return index === namespaceIndex && segment.slice(separator + 1) === name;
  }
  return segment === name;
}
async function resolvePath(port: BrowsePort, start: string, path: string): Promise<string> {
  const segments = path.split("/").filter(Boolean);
  if (segments.length === 0) throw new ContractRefusal(`browse_path "${path}" names no elements`);
  let current = path.startsWith("/") ? "ns=0;i=84" : canonicalNodeId(start);
  for (const segment of segments) {
    const refs = await port.children(current);
    const match = refs.find((ref) => browseNameMatches(segment, ref.namespaceIndex, ref.name));
    if (!match)
      throw new ContractRefusal(
        `browse_path "${path}" does not resolve: no child "${segment}" under ${current}`
      );
    current = match.nodeId;
  }
  return current;
}
export async function browseNodes(port: BrowsePort, request: BrowseRequest) {
  const limits = CONTRACT.traversal;
  const clamp = (value: number, lo: number, hi: number) =>
    Math.min(hi, Math.max(lo, Math.trunc(value)));
  const depth = clamp(request.depth ?? limits.defaultDepth, 0, limits.maxDepth);
  const maxNodes = clamp(request.maxNodes ?? limits.defaultMaxNodes, 1, limits.maxNodes);
  const root = request.browsePath
    ? await resolvePath(port, request.nodeId ?? limits.rootNodeId, request.browsePath)
    : canonicalNodeId(request.nodeId ?? limits.rootNodeId);
  const wantedClass = request.nodeClass?.toLowerCase();
  const wantedName = request.nameFilter?.toLowerCase();
  const keep = (record: NodeRefRecord) =>
    (wantedClass === undefined || record.node_class.toLowerCase() === wantedClass) &&
    (wantedName === undefined || record.browse_name.toLowerCase().includes(wantedName));
  try {
    const found: NodeRefRecord[] = [];
    let inspected = 0,
      truncated = false,
      unbrowsable = false;
    // Keep the existing Node root validation even for a nonzero traversal depth.
    const rootRecord = await port.describe(root, root);
    if (depth === 0) {
      inspected = 1;
      if (keep(rootRecord)) found.push(rootRecord);
    } else {
      const queue = [{ nodeId: root, depth: 0 }];
      const visited = new Set([root]);
      while (queue.length && !truncated) {
        const current = queue.shift()!;
        let refs: BrowseReference[];
        try {
          refs = await port.children(current.nodeId);
        } catch (error) {
          if (current.nodeId === root) throw error;
          unbrowsable = true;
          continue;
        }
        for (const ref of refs) {
          if (visited.has(ref.nodeId)) continue;
          visited.add(ref.nodeId);
          if (inspected >= maxNodes) {
            truncated = true;
            break;
          }
          inspected++;
          if (ref.name === limits.skipBrowseName) continue;
          const record: NodeRefRecord = {
            node_id: ref.nodeId,
            browse_name: `${ref.namespaceIndex}:${ref.name}`,
            node_class: ref.nodeClass,
            parent_node_id: current.nodeId,
            data_type: null,
            value: null,
            description: null,
            type_definition: null,
          };
          if (keep(record)) found.push(record);
          if (ref.nodeClass === "Object" && current.depth + 1 < depth)
            queue.push({ nodeId: ref.nodeId, depth: current.depth + 1 });
        }
      }
    }
    await port.enrich(found, request.includeValues ?? false);
    return {
      result: { nodes: found, truncated, inspected },
      completeness: traversalCompleteness({
        returned: found.length,
        truncated,
        maxNodes,
        unbrowsable,
      }),
    };
  } catch (error) {
    throw new ContractRefusal(
      message("browseFailed", { node_id: root, reason: describeError(error) }),
      { cause: error }
    );
  }
}
