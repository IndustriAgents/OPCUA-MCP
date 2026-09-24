/** Whether a result is the whole answer, as a field (issue #137).
 *
 * `completeness.py` is the Python half. Every tool that can return fewer records
 * than its request covered puts one of these beside `result` in
 * structuredContent — the shape is `contract/tools.json` -> `completeness`. It
 * used to be prose: a trailing text block, deliberately kept out of the
 * structured result, so a client reading `structuredContent` could not tell a
 * capped history read from a complete one, nor a lossy event buffer from a quiet
 * plant.
 *
 * Free of any OPC UA type, like `limits.ts`, so both unit suites drive one table
 * through it: `tests/fixtures/completeness.json`. The builders below are the
 * only places either runtime decides what "complete" means for a tool.
 */

import { CONTRACT } from "./contract.js";
import { MAX_HISTORY_VALUES } from "./limits.js";

export type Reason =
  "requestLimit" | "contractLimit" | "serverLimit" | "bufferOverflow" | "unbrowsable";

export interface Completeness {
  complete: boolean;
  reasons: Reason[];
  returned: number;
  truncated: boolean;
  limit: number | null;
  dropped: number;
  remaining: boolean | null;
  continuation: Record<string, unknown> | null;
}

/** Assemble the object from its causes, in one fixed order.
 *
 * The order of `reasons` is truncation first, then loss, then what could not be
 * read — fixed so two runtimes reporting the same causes report the same array.
 */
function assemble(parts: {
  returned: number;
  truncatedBy?: Reason | null;
  limit?: number | null;
  dropped?: number;
  unbrowsable?: boolean;
  remaining: boolean | null;
  continuation?: Record<string, unknown> | null;
}): Completeness {
  const reasons: Reason[] = [];
  const truncated = Boolean(parts.truncatedBy);
  if (parts.truncatedBy) reasons.push(parts.truncatedBy);
  const dropped = parts.dropped ?? 0;
  if (dropped > 0) reasons.push("bufferOverflow");
  if (parts.unbrowsable) reasons.push("unbrowsable");
  return {
    complete: reasons.length === 0,
    reasons,
    returned: parts.returned,
    truncated,
    limit: truncated ? (parts.limit ?? null) : null,
    dropped,
    remaining: parts.remaining,
    continuation: truncated ? (parts.continuation ?? null) : null,
  };
}

/** A history read — raw values, stored events, or aggregate intervals.
 *
 * `fetched` is how many records the OPC UA server sent back, which for event
 * history is more than `returned` when `severity_min` filtered some out: a cap is
 * reached by what was fetched, not by what survived the filter. `wanted` is the
 * count that was asked for, or null for an aggregate read, which asks for none.
 *
 * Two pieces of evidence, and the first one found decides:
 *
 * - the count was reached. The server may well hold more, and whether it does is
 *   what the continuation point says — a server that returns none leaves the
 *   answer unknown, so `remaining` is null rather than a guess;
 * - the server returned a continuation point *short* of the count. That is the
 *   server's own limit, of a size this server cannot know.
 *
 * `nextStart` is where a forward read resumes: the last record's timestamp,
 * inclusive, so the boundary record comes back again rather than being skipped.
 * A backward read — the default when no start_time is given — cannot be resumed
 * from its arguments, so the caller passes null and `continuation` is null.
 */
export function historyCompleteness(parts: {
  returned: number;
  fetched: number;
  wanted: number | null;
  continuationPoint: boolean;
  nextStart: string | null;
}): Completeness {
  const { returned, fetched, wanted, continuationPoint, nextStart } = parts;
  const reachedCount = wanted !== null && wanted > 0 && fetched >= wanted;
  let truncatedBy: Reason | null = null;
  if (reachedCount) {
    truncatedBy = wanted >= MAX_HISTORY_VALUES ? "contractLimit" : "requestLimit";
  } else if (continuationPoint) {
    truncatedBy = "serverLimit";
  }
  return assemble({
    returned,
    truncatedBy,
    limit: reachedCount ? wanted : null,
    remaining: continuationPoint ? true : reachedCount ? null : false,
    continuation: nextStart ? { start_time: nextStart } : null,
  });
}

/** `read_events`: a drain of a bounded buffer, which can be short two ways.
 *
 * `remaining` events still buffered mean `limit` stopped the read, and calling
 * again unchanged returns the next of them — hence `continuation: {}`. `dropped`
 * events are the ones the buffer discarded before anyone read them; they are
 * reported, not recoverable.
 */
export function drainCompleteness(parts: {
  returned: number;
  limit: number;
  remaining: number;
  dropped: number;
}): Completeness {
  const more = parts.remaining > 0;
  return assemble({
    returned: parts.returned,
    truncatedBy: more ? "requestLimit" : null,
    limit: parts.limit,
    dropped: parts.dropped,
    remaining: more,
    continuation: {},
  });
}

/** `browse_opcua_nodes`: a bounded walk, which can stop early or have gaps.
 *
 * `maxNodes` is the budget in force after clamping. It is this server's own cap
 * when it is `traversal.maxNodes` — asking for more is clamped to it — and the
 * caller's otherwise. A walk cannot be resumed from its arguments, so there is
 * never a `continuation`: the way to see more is a larger budget or a narrower
 * root. `unbrowsable` is a node below the root that refused to list its
 * children; the walk carries on past it, and whatever was under it is missing.
 */
export function traversalCompleteness(parts: {
  returned: number;
  truncated: boolean;
  maxNodes: number;
  unbrowsable: boolean;
}): Completeness {
  const truncatedBy: Reason | null = parts.truncated
    ? parts.maxNodes >= CONTRACT.traversal.maxNodes
      ? "contractLimit"
      : "requestLimit"
    : null;
  return assemble({
    returned: parts.returned,
    truncatedBy,
    limit: parts.maxNodes,
    unbrowsable: parts.unbrowsable,
    remaining: parts.truncated,
  });
}

/** The subscription tools: whole records, each carrying its own ring buffer.
 *
 * Nothing truncates the list of subscriptions — there are at most
 * `limits.maxSubscriptions` of them. What can be lost is what each one buffered,
 * and each record says how much in its own `dropped`; this totals them, so a
 * client can test one field for the whole answer.
 */
export function bufferCompleteness(records: Array<{ dropped: number }>): Completeness {
  return assemble({
    returned: records.length,
    dropped: records.reduce((total, record) => total + record.dropped, 0),
    remaining: false,
  });
}
