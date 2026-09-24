// Which OPC UA statuses count as success.
import type { StatusCode } from "node-opcua-client";

/** Whether `statusCode` has Good *severity* — the top two bits clear (Part 4
 * §7.39) — rather than being exactly `Good`.
 *
 * `GoodClamped`, `GoodLocalOverride`, `GoodNoData` and the rest are successes
 * that say something more, and python-opcua's `is_good()` has always treated
 * them so. This runtime compared against `StatusCodes.Good`, so the same answer
 * was a value on one runtime and null on the other, a completed method call
 * reported as failed, an empty history range an error (#157). The subcode is
 * reported in the record's `status`, so treating it as success hides nothing.
 *
 * No status at all means Good in OPC UA.
 */
export function isGood(statusCode: StatusCode | null | undefined): boolean {
  if (statusCode === null || statusCode === undefined) return true;
  return (statusCode.value & 0xc0000000) === 0;
}
