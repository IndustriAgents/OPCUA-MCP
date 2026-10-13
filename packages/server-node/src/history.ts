/** OPC UA history continuation points: noticing them, and giving them back.
 *
 * `history.py` is the Python half. A server that holds more history than one
 * reply carries returns a continuation point (Part 11 §6.4.3, Part 4 §5.10.3),
 * and that is the only evidence a read has that it stopped short of the range
 * — the reason `completeness.serverLimit` exists (issue #137). Neither runtime
 * looked at it before, so a read the server cut short was reported as the whole
 * range.
 *
 * Raw reads release their points and offer stateless start-time arguments.
 * Aggregates consume points within the same call, preserving the original
 * interval anchor. No native point is exposed or kept across calls/reconnects.
 */

import {
  ClientSession,
  DataValue,
  HistoryReadResult,
  ReadProcessedDetails,
  HistoryReadRequest,
  ReadRawModifiedDetails,
  HistoryReadValueId,
  TimestampsToReturn,
  resolveNodeId,
} from "node-opcua-client";

import { CONTRACT } from "./contract.js";
import { ContractRefusal, message } from "./errors.js";
import { MAX_HISTORY_VALUES } from "./limits.js";
import { historyData } from "./records.js";

/** Raw stored readings, without the library's implicit bounding values. */
export function rawDetails(start: Date | undefined, end: Date | undefined, count: number) {
  return new ReadRawModifiedDetails({
    startTime: start,
    endTime: end,
    numValuesPerNode: count,
    returnBounds: CONTRACT.history.rawReturnBounds,
    isReadModified: false,
  });
}

/** Whether a history result carries a continuation point. */
export function continues(continuationPoint: Buffer | null | undefined): boolean {
  return Boolean(continuationPoint && continuationPoint.length > 0);
}

/** Release a continuation point, best-effort.
 *
 * `details` must be of the kind the original read sent — a server reads it to
 * know which history the point belongs to. A failure is swallowed: the read has
 * already succeeded, and the worst a lost release costs is the point staying
 * held until the session closes, which is what happened to every one before.
 */
export async function releaseContinuationPoint(
  session: ClientSession,
  nodeId: string,
  continuationPoint: Buffer | null | undefined,
  details: HistoryReadRequest["historyReadDetails"]
): Promise<void> {
  if (!continues(continuationPoint)) return;
  try {
    await session.historyRead(
      new HistoryReadRequest({
        historyReadDetails: details,
        releaseContinuationPoints: true,
        timestampsToReturn: TimestampsToReturn.Both,
        nodesToRead: [
          new HistoryReadValueId({
            nodeId: resolveNodeId(nodeId),
            continuationPoint: continuationPoint ?? undefined,
          }),
        ],
      })
    );
  } catch {
    // See above: best-effort by design.
  }
}

/** Match readAggregateValue's existing defaults, also for continuation/release. */
export function aggregateDetails(start: Date, end: Date, aggregateType: number, interval: number) {
  return new ReadProcessedDetails({
    startTime: start,
    endTime: end,
    aggregateType: [aggregateType],
    processingInterval: interval,
    aggregateConfiguration: {
      percentDataBad: 100,
      percentDataGood: 100,
      treatUncertainAsBad: true,
      useServerCapabilitiesDefaults: true,
      useSlopedExtrapolation: false,
    },
  });
}

/** Resume on the same session using the original processed-history details. */
export async function readContinuation(
  session: ClientSession,
  nodeId: string,
  point: Buffer,
  details: HistoryReadRequest["historyReadDetails"]
): Promise<HistoryReadResult> {
  const response = await session.historyRead(
    new HistoryReadRequest({
      historyReadDetails: details,
      timestampsToReturn: TimestampsToReturn.Both,
      releaseContinuationPoints: false,
      nodesToRead: [
        new HistoryReadValueId({ nodeId: resolveNodeId(nodeId), continuationPoint: point }),
      ],
    })
  );
  if (response.results?.length !== 1) {
    throw new Error("Read aggregate failed: expected one history result");
  }
  return response.results[0];
}

/** Drain a bounded aggregate query; never return an unfinished range.
 * Each continuation page must add a value, bounding requests by maxValues.
 * Failures/cancellation release the currently held point best-effort.
 */
export async function aggregatePages(
  first: HistoryReadResult,
  readNext: (point: Buffer) => Promise<HistoryReadResult>,
  release: (point: Buffer) => Promise<void>,
  maxValues = MAX_HISTORY_VALUES
): Promise<DataValue[]> {
  let held: Buffer | null | undefined = first.continuationPoint;
  let result = first;
  const values: DataValue[] = [];
  try {
    while (true) {
      const point = result.continuationPoint;
      if (continues(point)) held = point;
      const page = historyData<DataValue>(result, "Read aggregate", "dataValues");
      if (values.length + page.length > maxValues) {
        throw new ContractRefusal(message("aggregatePageLimit", { limit: maxValues }));
      }
      values.push(...page);
      if (!continues(point)) {
        held = null;
        return values;
      }
      if (page.length === 0) throw new ContractRefusal(message("aggregateNoProgress"));
      if (values.length === maxValues) {
        throw new ContractRefusal(message("aggregatePageLimit", { limit: maxValues }));
      }
      result = await readNext(point!);
    }
  } finally {
    if (continues(held)) {
      try {
        await release(held!);
      } catch {
        /* Preserve the primary failure. */
      }
    }
  }
}
