// The canonical record shape for the history family of tools.
//
// `contract/tools.json` -> resultShapes.historyRecords is the specification;
// this module is the Node implementation of it, and `records.py` in the Python
// server is the other. Both servers must emit the same record for the same
// reading: a client — or a model — that has learned one server's output has to
// be able to read the other's. See issue #23.
//
// Conversion keys on the OPC UA *data type*, not on the JavaScript runtime type.
// That is not incidental. The two client libraries represent the same OPC UA
// value with wildly different native types — a ByteString is a Buffer here and
// `bytes` in Python, an Int64 is a [high, low] pair here and a plain int in
// Python — so anything that reaches for `String(value)` or `typeof` as its
// primary signal diverges by construction. The per-type table is shared, and
// pinned from both sides by tests/fixtures/value-encoding.json.
import { DataType, DataValue, Variant, VariantArrayType } from "node-opcua-client";

export interface HistoryRecord {
  /** JSON-native where the OPC UA type allows; see `variantToJson`. */
  value: unknown;
  /** ISO-8601 UTC with a trailing `Z`, or null when the server supplied none. */
  timestamp: string | null;
  /** OPC UA status code name, e.g. `Good` / `BadNoData`. */
  status: string;
}

/** Past this, a JSON number silently loses digits in any JS parser. */
const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

/** The OPC UA type name (`"Double"`, `"ByteString"`, …) both runtimes key on. */
function typeName(dataType: DataType | undefined | null): string {
  return dataType === undefined || dataType === null ? "" : (DataType[dataType] ?? "");
}

/** A 64-bit integer as a JSON number, or a decimal string when it cannot be one.
 *
 * Both servers switch to a string at the same threshold, so neither rounds a
 * counter or a serial number behind the caller's back.
 */
function bigIntToJson(value: bigint): number | string {
  return value >= -MAX_SAFE && value <= MAX_SAFE ? Number(value) : value.toString();
}

/** An Int64/UInt64, which node-opcua carries as a [high, low] pair of halves.
 *
 * A JS number cannot hold the full 64-bit range, so node-opcua splits it in two.
 * python-opcua hands over a plain int, so without this the same reading is `-5`
 * on one server and `[4294967295, 4294967291]` on the other.
 */
function int64ToJson(value: unknown, signed: boolean): unknown {
  let big: bigint;

  if (Array.isArray(value) && value.length === 2) {
    const [high, low] = value as [number, number];
    big = (BigInt(high >>> 0) << 32n) | BigInt(low >>> 0);
    big = signed ? BigInt.asIntN(64, big) : BigInt.asUintN(64, big);
  } else if (typeof value === "bigint") {
    big = value;
  } else if (typeof value === "number") {
    return Number.isFinite(value) ? value : String(value);
  } else {
    return String(value);
  }

  return bigIntToJson(big);
}

/** One scalar OPC UA value as JSON, given its data type. */
function scalarToJson(value: unknown, dataType: DataType | undefined | null): unknown {
  if (value === null || value === undefined) return null;

  // Types whose native representation differs between the two client libraries.
  // NodeId, ExpandedNodeId and QualifiedName are absent on purpose: node-opcua
  // already stringifies those to their canonical `ns=2;i=3` / `2:Name` forms,
  // which is what the Python server produces too.
  switch (typeName(dataType)) {
    case "Int64":
      return int64ToJson(value, true);
    case "UInt64":
      return int64ToJson(value, false);
    case "ByteString":
      return Buffer.isBuffer(value) ? value.toString("base64") : String(value);
    case "DateTime":
      return toIsoUtc(value as Date) ?? String(value);
    case "Guid":
      // Lower case: the RFC 4122 canonical form, and what Python's UUID renders.
      return String(value).toLowerCase();
    case "StatusCode":
      return (value as { name?: string }).name ?? String(value);
    case "LocalizedText":
      // The text only; the locale is not part of the reading.
      return (value as { text?: string | null }).text ?? "";
  }

  switch (typeof value) {
    case "boolean":
    case "string":
      return value;
    case "number":
      // NaN and ±Infinity are not JSON; JSON.stringify would silently emit
      // `null` and lose the distinction between "no data" and "not a number".
      return Number.isFinite(value) ? value : String(value);
    case "bigint":
      return bigIntToJson(value);
  }

  // Fallbacks for a value that arrived without a usable data type.
  if (value instanceof Date) return toIsoUtc(value) ?? String(value);
  if (Buffer.isBuffer(value)) return value.toString("base64");
  if (Array.isArray(value) || ArrayBuffer.isView(value)) {
    return Array.from(value as ArrayLike<unknown>, (item) => scalarToJson(item, dataType));
  }

  // Structured and opaque types (ExtensionObject, XmlElement, …) degrade to a
  // string. Those are not the shape of anything a server historises as a
  // variable value, and a faithful cross-runtime encoding of them would be a
  // much larger undertaking than this record is worth.
  return String(value);
}

/** An OPC UA value as JSON, without the Variant wrapper.
 *
 * Values that JSON can carry pass through as themselves, so a consumer gets
 * `51.75` rather than `"51.75"`. Everything else follows the per-type table in
 * `scalarToJson`.
 */
export function variantToJson(variant: Variant | null | undefined): unknown {
  if (!variant) return null;

  const { value, dataType, arrayType } = variant;
  if (value === null || value === undefined) return null;

  // Array-ness comes from the variant, never from the runtime type: a *scalar*
  // Int64 is itself a two-element array, and a scalar ByteString is a Buffer,
  // so `Array.isArray` / `ArrayBuffer.isView` would split both down the middle.
  if (arrayType !== undefined && arrayType !== VariantArrayType.Scalar) {
    return Array.from(value as ArrayLike<unknown>, (item) => scalarToJson(item, dataType));
  }

  return scalarToJson(value, dataType);
}

/** A timestamp as ISO-8601 UTC, mirroring the Python server's `format_iso_utc`. */
export function toIsoUtc(value: Date | null | undefined): string | null {
  if (!(value instanceof Date) || isNaN(value.getTime())) return null;
  return value.toISOString();
}

/** One `DataValue` as a canonical record. */
export function toHistoryRecord(dataValue: DataValue): HistoryRecord {
  return {
    value: variantToJson(dataValue?.value),
    timestamp: toIsoUtc(dataValue?.sourceTimestamp),
    // An absent status code means Good in OPC UA, so say so rather than null.
    status: dataValue?.statusCode?.name ?? "Good",
  };
}

/** The history/aggregate `DataValue` array as canonical records. */
export function toHistoryRecords(dataValues: DataValue[] | null | undefined): HistoryRecord[] {
  return (dataValues ?? []).map(toHistoryRecord);
}
