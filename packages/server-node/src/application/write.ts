/** Write orchestration. One per-call port prepares values and sends at most one service. */
import { ContractRefusal, AdapterFailure, describeError, message } from "../errors.js";
import { canonicalNodeId } from "../node-ids.js";
import { asNumber, formatNumber } from "../policy.js";

export interface WriteRequest {
  node_id: string;
  value: unknown;
  data_type?: string;
}
export interface WriteRecord {
  node_id: string;
  status: string;
  error: string | null;
}
export interface CurrentValue {
  good: boolean;
  status: string;
  number: number | null;
}
export interface WriteEngineering {
  unit: string | null;
  eu_range: { low: number; high: number } | null;
}
export interface WritePort {
  current(
    nodes: WriteRequest[],
    indices: number[],
    chunk: number
  ): Promise<Map<number, CurrentValue>>;
  engineering(nodeIds: string[]): Promise<Map<string, WriteEngineering | null>>;
  prepare(node: WriteRequest, index: number): Promise<{ status: string; error: string } | null>;
  send(): Promise<string[]>;
}

export function checkEuRange(nodeId: string, value: unknown, info: WriteEngineering | null): void {
  const range = info?.eu_range;
  if (!range) return;
  const number = asNumber(value);
  if (number === null || (range.low <= number && number <= range.high)) return;
  throw new ContractRefusal(
    message("valueOutOfRange", {
      value: formatNumber(number),
      node_id: nodeId,
      low: formatNumber(range.low),
      high: formatNumber(range.high),
      unit: info?.unit ? ` ${info.unit}` : "",
      source: "the OPC UA server's own EURange",
    })
  );
}

export function checkMaxChange(
  nodeId: string,
  value: unknown,
  limit: number,
  current?: CurrentValue
): void {
  const present = current?.number ?? null;
  if (present === null) {
    const reason = !current
      ? "it could not be read"
      : !current.good
        ? current.status
        : "the node returned no usable value";
    throw new ContractRefusal(message("currentValueUnreadable", { node_id: nodeId, reason }));
  }
  const wanted = asNumber(value);
  if (wanted === null)
    throw new ContractRefusal(
      message("valueNotComparable", {
        node_id: nodeId,
        value: JSON.stringify(value) ?? String(value),
      })
    );
  const change = Math.abs(wanted - present);
  if (change > limit)
    throw new ContractRefusal(
      message("valueChangeTooLarge", {
        node_id: nodeId,
        current: formatNumber(present),
        value: formatNumber(wanted),
        change: formatNumber(change),
        limit: formatNumber(limit),
      })
    );
}

export async function writeNodes(
  port: WritePort,
  nodes: WriteRequest[],
  limits: { write: number; read: number },
  bounds: Map<number, number | null>,
  allowOutOfRange: boolean
): Promise<WriteRecord[]> {
  if (!Array.isArray(nodes) || nodes.length === 0)
    throw new ContractRefusal(
      message("emptyArray", { tool: "write_opcua_nodes", argument: "nodes" })
    );
  if (nodes.length > limits.write)
    throw new ContractRefusal(
      message("tooManyWritesForServer", {
        tool: "write_opcua_nodes",
        count: nodes.length,
        limit: limits.write,
      })
    );
  try {
    const results = nodes.map((node) => ({
      node_id: canonicalNodeId(String(node?.node_id ?? "")),
      status: "Good",
      error: null as string | null,
    }));
    const indices = nodes
      .map((node, index) => ({ node, index }))
      .filter(({ node, index }) => !node?.data_type || bounds.get(index) != null)
      .map(({ index }) => index);
    const current = indices.length
      ? await port.current(nodes, indices, limits.read)
      : new Map<number, CurrentValue>();
    const engineering = allowOutOfRange
      ? new Map<string, WriteEngineering | null>()
      : await port.engineering(nodes.map((node) => String(node?.node_id ?? "")));
    nodes.forEach((node, index) => {
      for (const value of Array.isArray(node?.value) ? node.value : [node?.value])
        checkEuRange(node.node_id, value, engineering.get(node.node_id) ?? null);
      const maximumChange = bounds.get(index);
      if (maximumChange != null)
        checkMaxChange(node.node_id, node.value, maximumChange, current.get(index));
    });
    const prepared: number[] = [];
    for (let index = 0; index < nodes.length; index++) {
      const failure = await port.prepare(nodes[index], index);
      if (failure) results[index] = { ...results[index], ...failure };
      else prepared.push(index);
    }
    if (prepared.length) {
      const statuses = await port.send();
      statuses.forEach((status, position) => {
        results[prepared[position]].status = status;
      });
    }
    return results;
  } catch (error) {
    if (error instanceof ContractRefusal) throw error;
    throw new AdapterFailure(
      "write",
      message("writeFailed", { reason: describeError(error) }),
      error
    );
  }
}
