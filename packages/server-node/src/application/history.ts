/** History query semantics over normalized records; native pages stay inside the port. */
import { AdapterFailure, ContractRefusal, describeError, message } from "../errors.js";
import { toDate } from "../dates.js";
import { MAX_HISTORY_VALUES, aggregateIntervals, historyValues } from "../limits.js";
import { formatNumber } from "../policy.js";
import { historyCompleteness } from "../completeness.js";
export interface HistoryRequest {
  nodeId: string;
  start?: string;
  end?: string;
  numValues: number;
  aggregateFunction?: string;
  processingInterval: number;
}
export interface HistoryRecord {
  timestamp: string | null;
  value: unknown;
}
export interface HistoryPort {
  raw(
    nodeId: string,
    start: Date | undefined,
    end: Date | undefined,
    wanted: number
  ): Promise<{ records: HistoryRecord[]; continued: boolean }>;
  aggregate(
    nodeId: string,
    start: Date,
    end: Date,
    name: string,
    interval: number
  ): Promise<HistoryRecord[]>;
}
export function forwardFrom(
  start: Date | undefined,
  end: Date | undefined,
  last: string | null | undefined
): string | null {
  if (!start || (end && end.getTime() <= start.getTime())) return null;
  return typeof last === "string" ? last : null;
}
export async function readHistory(
  port: HistoryPort,
  request: HistoryRequest,
  offered: string[],
  now = () => new Date()
) {
  const { nodeId, aggregateFunction } = request;
  if (aggregateFunction !== undefined && request.start === undefined)
    throw new ContractRefusal(message("aggregateNeedsStart"));
  try {
    if (aggregateFunction === undefined) {
      const wanted = historyValues(request.numValues);
      const start = toDate(request.start);
      const end = toDate(request.end);
      const page = await port.raw(nodeId, start, end, wanted);
      return {
        records: page.records,
        completeness: historyCompleteness({
          returned: page.records.length,
          fetched: page.records.length,
          wanted,
          continuationPoint: page.continued,
          nextStart: forwardFrom(start, end, page.records.at(-1)?.timestamp),
        }),
      };
    }
    if (!offered.includes(aggregateFunction))
      throw new ContractRefusal(
        offered.length === 0
          ? "Server does not advertise any aggregate functions"
          : `Invalid aggregate function. Supported: ${offered.join(", ")}`
      );
    const start = toDate(request.start)!;
    const end = toDate(request.end) ?? now();
    const intervals = aggregateIntervals(
      start.getTime(),
      end.getTime(),
      request.processingInterval
    );
    if (intervals > MAX_HISTORY_VALUES)
      throw new ContractRefusal(
        message("tooManyIntervals", {
          tool: "read_opcua_history",
          count: intervals,
          processing_interval: formatNumber(request.processingInterval),
          limit: MAX_HISTORY_VALUES,
        })
      );
    const records = await port.aggregate(
      nodeId,
      start,
      end,
      aggregateFunction,
      request.processingInterval
    );
    return {
      records,
      completeness: historyCompleteness({
        returned: records.length,
        fetched: records.length,
        wanted: null,
        continuationPoint: false,
        nextStart: null,
      }),
    };
  } catch (error) {
    if (error instanceof ContractRefusal) throw error;
    throw new AdapterFailure(
      "history",
      message("historyFailed", { node_id: nodeId, reason: describeError(error) }),
      error
    );
  }
}
