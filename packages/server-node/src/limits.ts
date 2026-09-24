/** How much one call may ask for, resolved from `contract/tools.json` -> `limits`.
 *
 * `limits.py` is the Python half. Free of any OPC UA type so both unit suites can
 * drive the same table through it, which is how the browse caps and the
 * subscription defaults are already kept in step.
 *
 * The caps are refusals rather than tuning knobs, and the one with any logic to
 * it is the history cap: `num_values: 0` used to mean "every reading in the
 * range", which against a node historised at 100ms is a request that never
 * returns. It now means "as many as allowed".
 */

import { CONTRACT } from "./contract.js";
import { ContractRefusal, message } from "./errors.js";

export const MAX_NODES_PER_READ = CONTRACT.limits.maxNodesPerRead;
export const MAX_NODES_PER_WRITE = CONTRACT.limits.maxNodesPerWrite;
export const MAX_METHOD_ARGUMENTS = CONTRACT.limits.maxMethodArguments;
export const MAX_HISTORY_VALUES = CONTRACT.limits.maxHistoryValues;
export const MAX_SUBSCRIPTIONS = CONTRACT.limits.maxSubscriptions;
export const MAX_EVENT_BUFFER_SIZE = CONTRACT.limits.maxEventBufferSize;
export const MAX_REQUEST_BYTES = CONTRACT.limits.maxRequestBytes;
export const MAX_STRING_BYTES = CONTRACT.limits.maxStringBytes;
export const MAX_BYTE_STRING_BYTES = CONTRACT.limits.maxByteStringBytes;
export const MAX_ARRAY_ITEMS = CONTRACT.limits.maxArrayItems;
export const MAX_NESTING_DEPTH = CONTRACT.limits.maxNestingDepth;

/** How many raw readings to ask the OPC UA server for.
 *
 * `0`, absent, or anything past the cap resolves to the cap. A negative number
 * is not a smaller request than zero — it is not a request at all — so it
 * resolves the same way.
 */
export function historyValues(numValues: number | null | undefined): number {
  if (!numValues || numValues < 0) return MAX_HISTORY_VALUES;
  return Math.min(Math.trunc(numValues), MAX_HISTORY_VALUES);
}

/** The event buffer `subscribe_events` will actually keep.
 *
 * A clamp rather than a refusal, like the data-change buffer beside it: the tool
 * already reports the size it applied, so a caller that asked for more is told
 * what it got. 0 or absent is the default, as it always was.
 */
export function eventBufferSize(requested: number | null | undefined): number {
  if (!requested || requested < 0) return CONTRACT.events.defaults.bufferSize;
  return Math.min(Math.trunc(requested), MAX_EVENT_BUFFER_SIZE);
}

/** The UTF-8 length of a string, which is what a limit on one is a limit on.
 *
 * Bytes rather than characters because bytes are what get encoded and sent: a
 * string of 'é' is twice as large on the wire as its length says. A lone
 * surrogate counts as three, which is what Python's `surrogatepass` counts too.
 */
export function utf8Bytes(text: string): number {
  return Buffer.byteLength(text, "utf8");
}

/** Refuse a call whose arguments are larger than any tool needs (issue #139).
 *
 * Every tool, before its arguments are validated, authorized or converted to
 * anything OPC UA: a request that fails here never reaches the policy layer's
 * per-value checks, never allocates a Variant and never opens a service call.
 * Walked before the validator because the validator's work grows with the
 * request, and this walk stops at the first thing out of bounds — an array of a
 * million items is refused on its length, not after its millionth element.
 *
 * The walk is also what makes the size check safe to run: the nesting bound is
 * enforced while descending, so recursion here is at most `MAX_NESTING_DEPTH`
 * deep, and `JSON.stringify` below it only ever sees a shallow document.
 *
 * `limits.py` walks in the same order and words each refusal from the same
 * template, and `tests/fixtures/request-limits.json` holds the two to it.
 */
export function checkRequestBounds(tool: string, args: unknown): void {
  walk(tool, args, "", 0);
  const size = utf8Bytes(JSON.stringify(args) ?? "");
  if (size > MAX_REQUEST_BYTES) {
    throw new ContractRefusal(message("requestTooLarge", { tool, size, limit: MAX_REQUEST_BYTES }));
  }
}

function walk(tool: string, value: unknown, path: string, depth: number): void {
  if (typeof value === "string") {
    const size = utf8Bytes(value);
    if (size > MAX_STRING_BYTES) {
      throw new ContractRefusal(
        message("stringTooLong", {
          tool,
          argument: path || "arguments",
          size,
          limit: MAX_STRING_BYTES,
        })
      );
    }
    return;
  }
  if (typeof value !== "object" || value === null) return;

  // The arguments object itself is level 1, so the count is one a caller can
  // work out from what they sent without knowing how this walk is written.
  if (depth + 1 > MAX_NESTING_DEPTH) {
    throw new ContractRefusal(
      message("nestedTooDeep", { tool, argument: path || "arguments", limit: MAX_NESTING_DEPTH })
    );
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_ARRAY_ITEMS) {
      throw new ContractRefusal(
        message("arrayTooLong", {
          tool,
          argument: path || "arguments",
          count: value.length,
          limit: MAX_ARRAY_ITEMS,
        })
      );
    }
    value.forEach((item, index) => walk(tool, item, `${path}[${index}]`, depth + 1));
    return;
  }
  for (const [key, item] of Object.entries(value)) {
    walk(tool, item, path ? `${path}.${key}` : key, depth + 1);
  }
}

/** The bound that applies to one kind of service call: ours, or the server's.
 *
 * A server's OperationLimits can only lower a project limit, never raise it —
 * the project limit is what this server has decided one call may cost, whatever
 * the other end would tolerate. 0 is the spec's "no limit", and an unreadable
 * node is the same answer: the project limit stands.
 */
export function effectiveLimit(project: number, server: number | null | undefined): number {
  if (typeof server !== "number" || !Number.isFinite(server) || server <= 0) return project;
  return Math.min(project, Math.trunc(server));
}

/** `items` in consecutive runs of at most `size`, in order. */
export function chunked<T>(items: T[], size: number): T[][] {
  const step = Math.max(1, Math.trunc(size));
  const chunks: T[][] = [];
  for (let start = 0; start < items.length; start += step) {
    chunks.push(items.slice(start, start + step));
  }
  return chunks;
}

/** How many intervals an aggregate read asks the OPC UA server to compute.
 *
 * `processing_interval` 0 is the whole range as one interval. The count is what
 * the server would have to produce, and what the reply would carry, so it is
 * bounded the same way a raw read is — by `limits.maxHistoryValues`.
 */
export function aggregateIntervals(startMs: number, endMs: number, intervalMs: number): number {
  if (!(intervalMs > 0)) return 1;
  return Math.max(1, Math.ceil(Math.abs(endMs - startMs) / intervalMs));
}
