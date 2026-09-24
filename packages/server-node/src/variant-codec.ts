// Strict MCP JSON to OPC UA Variant conversion for writes.
//
// What a JSON value becomes on the wire is decided here, and it has to be
// decided the same way by `variant_codec.py`: a value one runtime writes and the
// other refuses — or, worse, writes differently — is a different plant depending
// on which package was installed (#157). tests/fixtures/write-coercion.json is
// the one table both are held to, messages included.
//
// The rules, where each runtime's native conversion used to decide:
//
// - A numeric string follows JSON's number grammar (`numeric.ts`); no "0x10",
//   "1_000", "inf" or "" (which `Number()` made 0).
// - A boolean is not a number and a number is not a string: `true` is refused
//   for a Double and `42` for a String, rather than each runtime spelling it its
//   own way ("true"/"True", "1"/"1.0").
// - A scalar node takes a scalar. `[5]` is not 5, although `String([5])` is "5".
// - An integer given as a JSON number must be one JSON carries exactly
//   (±2^53-1); a larger one has already been rounded by the time it gets here,
//   so it is refused and must be sent as a decimal string.
import { DataType, VariantArrayType, coerceNodeId } from "node-opcua-client";

import { toDate } from "./dates.js";
import { ContractRefusal, message } from "./errors.js";
import { MAX_BYTE_STRING_BYTES } from "./limits.js";
import { exactInteger, numericText } from "./numeric.js";

const INTEGER_RANGES = new Map<DataType, [bigint, bigint]>([
  [DataType.SByte, [-128n, 127n]],
  [DataType.Byte, [0n, 255n]],
  [DataType.Int16, [-32768n, 32767n]],
  [DataType.UInt16, [0n, 65535n]],
  [DataType.Int32, [-2147483648n, 2147483647n]],
  [DataType.UInt32, [0n, 4294967295n]],
  [DataType.Int64, [-(1n << 63n), (1n << 63n) - 1n]],
  [DataType.UInt64, [0n, (1n << 64n) - 1n]],
]);

/** The largest finite IEEE-754 single. Past it a Float is Infinity on the wire —
 * node-opcua writes that, python-opcua raises — so it is refused before either. */
const FLOAT_MAX = 3.4028234663852886e38;

const GUID = /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const JSON_WHITESPACE = /^[ \t\n\r]+|[ \t\n\r]+$/g;

function typeName(dataType: DataType): string {
  return DataType[dataType] ?? String(dataType);
}

/** `raw` as JSON, for echoing in a message; `json_text` in Python writes the same. */
function jsonText(raw: unknown): string {
  return JSON.stringify(raw) ?? String(raw);
}

function cannot(raw: unknown, dataType: DataType): Error {
  return new Error(`Cannot convert ${jsonText(raw)} to ${typeName(dataType)}`);
}

function outOfRange(text: string, dataType: DataType): Error {
  return new Error(`${text} is outside the ${typeName(dataType)} range`);
}

function integer(raw: unknown, dataType: DataType): number | [number, number] {
  const [minimum, maximum] = INTEGER_RANGES.get(dataType)!;
  const wide = dataType === DataType.Int64 || dataType === DataType.UInt64;
  let value: bigint;

  if (typeof raw === "number") {
    if (!Number.isFinite(raw)) {
      throw new Error(`The number is outside the ${typeName(dataType)} range`);
    }
    if (!Number.isInteger(raw)) throw cannot(raw, dataType);
    if (!Number.isSafeInteger(raw) && wide) {
      throw new Error(
        `A JSON number beyond ±${Number.MAX_SAFE_INTEGER} has lost precision before it ` +
          `arrives; send ${typeName(dataType)} values this large as decimal strings`
      );
    }
    value = BigInt(raw);
    if (value < minimum || value > maximum) throw outOfRange(jsonText(raw), dataType);
  } else {
    const text = typeof raw === "string" ? numericText(raw) : null;
    const exact = text === null ? null : exactInteger(text);
    if (exact === null) throw cannot(raw, dataType);
    if (exact === "too large" || exact < minimum || exact > maximum) {
      throw outOfRange(text!, dataType);
    }
    value = exact;
  }

  if (!wide) return Number(value);
  const unsigned = value < 0 ? value + (1n << 64n) : value;
  return [Number((unsigned >> 32n) & 0xffffffffn), Number(unsigned & 0xffffffffn)];
}

function boolean(raw: unknown): boolean {
  if (typeof raw === "boolean") return raw;
  if (typeof raw === "number" && (raw === 0 || raw === 1)) return raw === 1;
  if (typeof raw === "string") {
    const normalized = raw.replace(JSON_WHITESPACE, "").toLowerCase();
    if (["true", "1", "yes", "on"].includes(normalized)) return true;
    if (["false", "0", "no", "off"].includes(normalized)) return false;
  }
  throw cannot(raw, DataType.Boolean);
}

function bytes(raw: unknown): Buffer {
  let decoded: Buffer;
  if (Buffer.isBuffer(raw)) {
    decoded = raw;
  } else if (typeof raw !== "string" || !BASE64.test(raw)) {
    throw new Error("ByteString values must be standard base64");
  } else {
    decoded = Buffer.from(raw, "base64");
  }
  // A refusal of the request, not a conversion failure of one value: a write
  // reports a conversion failure as that node's status and sends the rest,
  // while this has to stop the batch before anything is sent (issue #139).
  if (decoded.length > MAX_BYTE_STRING_BYTES) {
    throw new ContractRefusal(
      message("byteStringTooLong", { size: decoded.length, limit: MAX_BYTE_STRING_BYTES })
    );
  }
  return decoded;
}

function floating(raw: unknown, dataType: DataType): number {
  let value: number;
  if (typeof raw === "number") {
    if (!Number.isFinite(raw)) {
      throw new Error(`The number is outside the ${typeName(dataType)} range`);
    }
    value = raw;
  } else {
    const text = typeof raw === "string" ? numericText(raw) : null;
    if (text === null) throw cannot(raw, dataType);
    value = Number(text);
    if (!Number.isFinite(value)) throw outOfRange(text, dataType);
  }
  if (dataType === DataType.Float && Math.abs(value) > FLOAT_MAX) {
    throw outOfRange(String(value), dataType);
  }
  return value;
}

function stringOnly(raw: unknown, dataType: DataType): string {
  if (typeof raw !== "string") {
    throw new Error(`${typeName(dataType)} values must be a JSON string`);
  }
  return raw;
}

function scalar(raw: unknown, dataType: DataType): unknown {
  if (raw === null || raw === undefined) {
    throw new Error(`Cannot write null as ${typeName(dataType)}`);
  }
  if (INTEGER_RANGES.has(dataType)) return integer(raw, dataType);

  switch (dataType) {
    case DataType.Boolean:
      return boolean(raw);
    case DataType.Float:
    case DataType.Double:
      return floating(raw, dataType);
    case DataType.String:
      return stringOnly(raw, dataType);
    case DataType.DateTime:
      if (raw instanceof Date) return raw;
      if (typeof raw !== "string") {
        throw new Error("DateTime values must be an ISO 8601 string, e.g. 2026-04-23T17:40:00Z");
      }
      return toDate(raw);
    case DataType.Guid:
      if (typeof raw !== "string" || !GUID.test(raw)) {
        throw new Error(`${jsonText(raw)} is not a Guid`);
      }
      return raw.toLowerCase();
    case DataType.ByteString:
      return bytes(raw);
    case DataType.NodeId:
      return coerceNodeId(stringOnly(raw, dataType));
    case DataType.LocalizedText:
      return { text: stringOnly(raw, dataType) };
    case DataType.QualifiedName:
      return { name: stringOnly(raw, dataType) };
    default:
      throw new Error(`Writes to OPC UA ${typeName(dataType)} values are not supported safely`);
  }
}

/** Convert an MCP JSON value using the target node's OPC UA Variant metadata. */
export function convertForVariant(
  raw: unknown,
  dataType: DataType,
  arrayType: VariantArrayType = VariantArrayType.Scalar
): unknown {
  if (arrayType === VariantArrayType.Scalar) return scalar(raw, dataType);

  let values = raw;
  if (typeof values === "string") {
    try {
      values = JSON.parse(values);
    } catch {
      throw new Error(`${typeName(dataType)} array values must be a JSON array`);
    }
  }
  if (!Array.isArray(values)) {
    throw new Error(`${typeName(dataType)} array values must be a JSON array`);
  }
  return values.map((value) => scalar(value, dataType));
}
