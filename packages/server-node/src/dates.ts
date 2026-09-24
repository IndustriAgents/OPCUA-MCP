// Conversion between the ISO-8601 strings MCP delivers and Date objects.

/** The accepted grammar: an RFC 3339 date-time. The date and time are always
 * both there; seconds and a 1-9 digit fraction are optional; the separator is
 * `T`, `t` or a space; the zone is `Z`/`z` or `+HH:MM`/`-HH:MM`.
 *
 * Written out rather than left to `new Date()`, whose parser reads
 * `04/23/2026`, `April 23, 2026` and a bare `2026`, takes a zone-less string as
 * the *host's* local time, and rolls `2026-02-30` over to March 2 — none of it
 * what the Python runtime did (#157). `datetimes.py` is the other half, and
 * tests/fixtures/datetime-parsing.json holds both to the same table.
 *
 * The zone group is optional in the pattern only so a zone-less value can be
 * told apart from a malformed one: it is refused either way, with its own reason.
 */
const DATE_TIME =
  /^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?([Zz]|[+-][0-9]{2}:[0-9]{2})?$/;

const MS_PER_DAY = 86_400_000;
/** What an OPC UA DateTime can hold (Part 6 §5.2.2.5): 1601-01-01 is its zero,
 * and neither client library represents a year past 9999. */
const MIN_MS = -11_644_473_600_000; // 1601-01-01T00:00:00Z
const MAX_MS = 253_402_300_799_999; // 9999-12-31T23:59:59.999Z

function invalid(value: string): Error {
  return new Error(`Invalid date/time: "${value}". Use ISO 8601, e.g. 2026-04-23T17:40:00Z`);
}

function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    const isLeapYear = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    return isLeapYear ? 29 : 28;
  }
  return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
}

/** Days since 1970-01-01 in the proleptic Gregorian calendar.
 *
 * Plain integer arithmetic (Hinnant's algorithm), the same as `datetimes.py`,
 * so both runtimes reach the same instant without either calendar library in
 * the way — `Date.UTC` maps two-digit years into the 1900s.
 */
function daysFromCivil(year: number, month: number, day: number): number {
  const y = month <= 2 ? year - 1 : year;
  const era = Math.floor(y / 400);
  const yearOfEra = y - era * 400;
  const dayOfYear = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + day - 1;
  const dayOfEra =
    yearOfEra * 365 + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100) + dayOfYear;
  return era * 146097 + dayOfEra - 719468;
}

/** Parse an optional RFC 3339 date/time string into a Date.
 *
 * MCP delivers these as strings, so they must be converted before being handed
 * to node-opcua. Three refusals, worded identically by the Python server's
 * `parse_iso_datetime`:
 *
 * - anything outside the grammar above, or a date or time that does not exist;
 * - a value with no zone. Read as local time it depends on the host this server
 *   happens to run on; read as UTC it silently shifts a history window by the
 *   plant's offset from UTC. Neither is what the caller meant often enough to
 *   guess, and the fix is one character;
 * - an instant outside what an OPC UA DateTime can carry.
 *
 * A fraction is truncated to milliseconds, the precision a Date holds, and the
 * Python runtime truncates to the same so both send the plant the same instant.
 */
export function toDate(value: string | Date | undefined | null): Date | undefined {
  if (value === undefined || value === null) return undefined;
  if (value instanceof Date) return value;
  const match = typeof value === "string" ? DATE_TIME.exec(value) : null;
  if (!match) throw invalid(String(value));

  const [year, month, day, hour, minute] = match.slice(1, 6).map(Number);
  const second = Number(match[6] ?? "0");
  const millisecond = Number((match[7] ?? "").padEnd(3, "0").slice(0, 3));
  const zone = match[8];
  if (zone === undefined) {
    throw new Error(
      `Invalid date/time: "${value}" has no timezone, so the instant it names ` +
        "depends on where it is read. Add Z for UTC or an offset such as +02:00, " +
        "e.g. 2026-04-23T17:40:00Z"
    );
  }

  let offsetMinutes = 0;
  if (zone !== "Z" && zone !== "z") {
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) throw invalid(value);
    offsetMinutes = (offsetHour * 60 + offsetMinute) * (zone[0] === "-" ? -1 : 1);
  }
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysInMonth(year, month) ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    throw invalid(value);
  }

  const instant =
    daysFromCivil(year, month, day) * MS_PER_DAY +
    ((hour * 60 + minute - offsetMinutes) * 60 + second) * 1000 +
    millisecond;
  if (instant < MIN_MS || instant > MAX_MS) {
    throw new Error(
      `Invalid date/time: "${value}" is outside what an OPC UA DateTime can hold ` +
        "(1601-01-01T00:00:00Z to 9999-12-31T23:59:59Z)"
    );
  }
  return new Date(instant);
}
