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

export const MAX_NODES_PER_READ = CONTRACT.limits.maxNodesPerRead;
export const MAX_HISTORY_VALUES = CONTRACT.limits.maxHistoryValues;
export const MAX_SUBSCRIPTIONS = CONTRACT.limits.maxSubscriptions;

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

/** Whether a raw history read stopped at the per-call maximum.
 *
 * Only when the cap itself was reached: a caller who asked for 10 and got 10 has
 * what they asked for, and telling them the range may hold more would be noise
 * on every small read.
 */
export function historyWasClipped(returned: number, wanted: number): boolean {
  return wanted >= MAX_HISTORY_VALUES && returned >= MAX_HISTORY_VALUES;
}
