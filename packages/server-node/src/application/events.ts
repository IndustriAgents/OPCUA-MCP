/** Event subscription, drain and history semantics over a per-invocation port. */
import { AdapterFailure, ContractRefusal, describeError, message } from "../errors.js";
import { drainCompleteness, historyCompleteness } from "../completeness.js";
import { eventBufferSize, historyValues } from "../limits.js";
import { canonicalNodeId } from "../node-ids.js";
import { notice } from "../notices.js";
import { toDate } from "../dates.js";
import { forwardFrom } from "./history.js";
export interface EventDrain {
  records: unknown[];
  remaining: number;
  dropped: number;
  size: number;
  resubscribed: boolean;
}
export interface EventPage {
  records: unknown[];
  fetched: number;
  continued: boolean;
  lastTime: string | null;
}
export interface EventPort {
  subscribe(nodeId: string, severity: number, size: number): Promise<boolean>;
  drain(nodeId: string, limit: number): Promise<EventDrain | null>;
  history(
    nodeId: string,
    start: Date,
    end: Date,
    wanted: number,
    severity: number
  ): Promise<EventPage>;
}
export async function subscribeEvents(
  port: EventPort,
  nodeId: string,
  severity: number,
  requested: number
) {
  const size = eventBufferSize(requested);
  let replaced: boolean;
  try {
    replaced = await port.subscribe(nodeId, severity, size);
  } catch (error) {
    throw new AdapterFailure(
      "event-subscribe",
      message("eventSubscribeFailed", { node_id: nodeId, reason: describeError(error) }),
      error
    );
  }
  return { node_id: canonicalNodeId(nodeId), severity_min: severity, buffer_size: size, replaced };
}
export async function readEvents(port: EventPort, nodeId: string, limit: number) {
  const drained = await port.drain(nodeId, limit);
  if (!drained) throw new ContractRefusal(message("notSubscribedToEvents", { node_id: nodeId }));
  const notices = [];
  if (drained.dropped > 0)
    notices.push(notice("droppedEvents", { dropped: drained.dropped, buffer_size: drained.size }));
  if (drained.resubscribed) notices.push(notice("eventsResubscribed"));
  return {
    records: drained.records,
    completeness: drainCompleteness({
      returned: drained.records.length,
      limit,
      remaining: drained.remaining,
      dropped: drained.dropped,
    }),
    notices,
  };
}
export async function readEventHistory(
  port: EventPort,
  request: { nodeId: string; start?: string; end?: string; numValues: number; severityMin: number },
  now = () => new Date()
) {
  const end = toDate(request.end) ?? now();
  const start = toDate(request.start) ?? new Date(end.getTime() - 3600000);
  const wanted = historyValues(request.numValues);
  try {
    const page = await port.history(request.nodeId, start, end, wanted, request.severityMin);
    return {
      records: page.records,
      completeness: historyCompleteness({
        returned: page.records.length,
        fetched: page.fetched,
        wanted,
        continuationPoint: page.continued,
        nextStart: forwardFrom(start, end, page.lastTime),
      }),
    };
  } catch (error) {
    throw new AdapterFailure(
      "event-history",
      message("eventHistoryFailed", { node_id: request.nodeId, reason: describeError(error) }),
      error
    );
  }
}
