/** Write orchestration. One per-call port prepares values and sends at most one service. */
import { ContractRefusal, AdapterFailure, describeError, message } from "../errors.js";
import { factsGood, type NodeFacts } from "../node-facts.js";
import { canonicalNodeId } from "../node-ids.js";
import { asNumber, formatNumber, type Precondition } from "../policy.js";
import {
  EU_RANGE_SOURCE,
  INSTRUMENT_RANGE_SOURCE,
  outOfRangeMessage,
  planWrite,
  type WritePlan,
} from "../write-plan.js";
import { requireInterlocks, type InterlockPolicy, type InterlockPort } from "./interlocks.js";

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
/** What a node published about its number, as the write plan reads it. */
type PlanEngineering = Parameters<typeof planWrite>[2];
/** One write as planned: the value to convert, and the type and array-ness the
 *  node's attributes decided — null for either keeps the read-first inference. */
export interface PlannedWrite {
  node_id: string;
  value: unknown;
  data_type: string | null;
  array: boolean | null;
}
export interface WritePort extends InterlockPort {
  /** What each node says about itself; null where nothing could be read. Never throws. */
  facts(nodeIds: string[]): Promise<Map<string, NodeFacts | null>>;
  current(
    nodes: WriteRequest[],
    indices: number[],
    chunk: number
  ): Promise<Map<number, CurrentValue>>;
  engineering(nodeIds: string[]): Promise<Map<string, PlanEngineering>>;
  prepare(write: PlannedWrite, index: number): Promise<{ status: string; error: string } | null>;
  send(): Promise<string[]>;
}
/** The operator's interlocks, as the write path asks about them. */
export interface WriteInterlocks extends InterlockPolicy {
  preconditionsForNode(nodeId: string): Precondition[];
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

/** The refusal for a value outside the ranges the plant published, or null.
 *
 * `planWrite` checks the ranges only for a node whose facts were read; one whose
 * facts were not is still held to whatever range it published, as every write
 * was before the plans existed. An array is checked element by element: writing
 * [0, 9999] to a node whose range stops at 100 is writing 9999 to it.
 */
function rangeRefusal(
  nodeId: string,
  value: unknown,
  engineering: PlanEngineering,
  allowOutOfRange: boolean
): string | null {
  const euRange = allowOutOfRange ? null : (engineering?.eu_range ?? null);
  const instrumentRange = engineering?.instrument_range ?? null;
  const unit = engineering?.unit ?? null;
  for (const element of Array.isArray(value) ? value : [value]) {
    const number = asNumber(element);
    if (number === null) continue;
    const refusal =
      (euRange && outOfRangeMessage(nodeId, number, euRange, unit, EU_RANGE_SOURCE)) ||
      (instrumentRange &&
        outOfRangeMessage(nodeId, number, instrumentRange, unit, INSTRUMENT_RANGE_SOURCE));
    if (refusal) return refusal;
  }
  return null;
}

/** Plan every write from what its node says about itself (write-plan.ts). */
function planAll(
  nodes: WriteRequest[],
  nodeIds: string[],
  facts: Map<string, NodeFacts | null>,
  engineering: Map<string, PlanEngineering> | null,
  allowOutOfRange: boolean
): WritePlan[] {
  return nodes.map((node, index): WritePlan => {
    const nodeId = nodeIds[index];
    const known = facts.get(nodeId) ?? null;
    const info = engineering?.get(nodeId) ?? null;
    const plan = planWrite(
      { node_id: nodeId, value: node?.value, data_type: node?.data_type ?? null },
      known,
      info,
      { allow_out_of_range: allowOutOfRange }
    );
    if (plan.outcome !== "send" || factsGood(known)) return plan;
    const refusal = rangeRefusal(nodeId, plan.value, info, allowOutOfRange);
    return refusal === null ? plan : { outcome: "refuse", error: refusal };
  });
}

/** `write_opcua_nodes`: plan each write, check it, then send the batch as one Write.
 *
 * Each write is planned from what its node says about itself before anything is
 * sent: the type it is converted to comes from the node's DataType attribute, so
 * a write-only node is writable without reading it, and only a node whose type
 * is still unknown — or that carries a `max_change` bound, which is a bound on
 * the *move* — has its current value read, in one read for both. A node that
 * cannot take the write — an Object, a read-only node, a list for a scalar — is
 * that node's record and nothing is sent to it; a value that is not one of its
 * states, or is outside the range the plant published, refuses the whole batch,
 * so it can never end up partially applied. Then the operator's interlocks, for
 * the writes that will actually go out.
 */
export async function writeNodes(
  port: WritePort,
  nodes: WriteRequest[],
  limits: { write: number; read: number },
  bounds: Map<number, number | null>,
  allowOutOfRange: boolean,
  interlocks: WriteInterlocks | null = null
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
    const nodeIds = nodes.map((node) => String(node?.node_id ?? ""));
    const results = nodeIds.map((nodeId) => ({
      node_id: canonicalNodeId(nodeId),
      status: "Good",
      error: null as string | null,
    }));
    const facts = await port.facts(nodeIds);
    // A plan's type and shape never depend on the plant's ranges — only whether
    // it is refused does — so which nodes need their current value is known
    // before the engineering record is.
    const indices = planAll(nodes, nodeIds, facts, null, allowOutOfRange).flatMap((plan, index) =>
      plan.outcome === "send" && (plan.data_type === null || bounds.get(index) != null)
        ? [index]
        : []
    );
    const current = indices.length
      ? await port.current(nodes, indices, limits.read)
      : new Map<number, CurrentValue>();
    // Read even when the EURange check is lifted: the InstrumentRange is not.
    const engineering = await port.engineering(nodeIds);
    const plans = planAll(nodes, nodeIds, facts, engineering, allowOutOfRange);
    const refused = plans.find((plan) => plan.outcome === "refuse");
    if (refused?.outcome === "refuse") throw new ContractRefusal(refused.error);

    if (interlocks !== null) {
      await requireInterlocks(
        port,
        interlocks,
        nodeIds.flatMap((nodeId, index) =>
          plans[index].outcome === "send"
            ? [{ target: nodeId, preconditions: interlocks.preconditionsForNode(nodeId) }]
            : []
        )
      );
    }
    // Against the value that will be written — a state's label is its number by
    // now — and, like every bound, for the whole batch.
    plans.forEach((plan, index) => {
      const maximumChange = bounds.get(index);
      if (plan.outcome === "send" && maximumChange != null)
        checkMaxChange(nodeIds[index], plan.value, maximumChange, current.get(index));
    });

    const prepared: number[] = [];
    for (let index = 0; index < plans.length; index++) {
      const plan = plans[index];
      if (plan.outcome !== "send") {
        if (plan.outcome === "skip")
          results[index] = { ...results[index], status: plan.status, error: plan.error };
        continue;
      }
      const failure = await port.prepare(
        {
          node_id: nodeIds[index],
          value: plan.value,
          data_type: plan.data_type,
          array: plan.array,
        },
        index
      );
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
