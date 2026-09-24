/** OPC UA history continuation points: noticing them, and giving them back.
 *
 * `history.py` is the Python half. A server that holds more history than one
 * reply carries returns a continuation point (Part 11 §6.4.3, Part 4 §5.10.3),
 * and that is the only evidence a read has that it stopped short of the range
 * — the reason `completeness.serverLimit` exists (issue #137). Neither runtime
 * looked at it before, so a read the server cut short was reported as the whole
 * range.
 *
 * Nor did either give it back. A continuation point is state the server keeps
 * for this session until it is released or the session ends, and a server holds
 * only so many (MaxHistoryContinuationPoints) — so every read left one behind
 * until later reads started failing with BadNoContinuationPoints. This server
 * never resumes from one: `completeness.continuation` is stateless arguments
 * instead, which cannot go stale and survives a reconnect. So each is released
 * as soon as it has been noticed.
 */

import {
  ClientSession,
  HistoryReadRequest,
  HistoryReadValueId,
  TimestampsToReturn,
  resolveNodeId,
} from "node-opcua-client";

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
