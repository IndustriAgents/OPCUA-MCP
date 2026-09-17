import { DataType, VariantArrayType, coerceNodeId } from "node-opcua-client";

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

function typeName(dataType: DataType): string {
  return DataType[dataType] ?? String(dataType);
}

function integer(raw: unknown, dataType: DataType): number | [number, number] {
  const text = typeof raw === "bigint" ? raw.toString() : String(raw).trim();
  if (!/^[+-]?\d+$/.test(text)) {
    throw new Error(`Cannot convert ${JSON.stringify(raw)} to ${typeName(dataType)}`);
  }
  const value = BigInt(text);
  const [minimum, maximum] = INTEGER_RANGES.get(dataType)!;
  if (value < minimum || value > maximum) {
    throw new Error(`${text} is outside the ${typeName(dataType)} range`);
  }
  if (dataType !== DataType.Int64 && dataType !== DataType.UInt64) {
    return Number(value);
  }

  const unsigned = value < 0 ? value + (1n << 64n) : value;
  return [Number((unsigned >> 32n) & 0xffffffffn), Number(unsigned & 0xffffffffn)];
}

function boolean(raw: unknown): boolean {
  if (typeof raw === "boolean") return raw;
  const normalized = String(raw).trim().toLowerCase();
  if (["true", "1", "yes", "on"].includes(normalized)) return true;
  if (["false", "0", "no", "off"].includes(normalized)) return false;
  throw new Error(`Cannot convert ${JSON.stringify(raw)} to Boolean`);
}

function bytes(raw: unknown): Buffer {
  if (Buffer.isBuffer(raw)) return raw;
  if (
    typeof raw !== "string" ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(raw)
  ) {
    throw new Error("ByteString values must be standard base64");
  }
  return Buffer.from(raw, "base64");
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
    case DataType.Double: {
      const value = typeof raw === "number" ? raw : Number(String(raw).trim());
      if (!Number.isFinite(value)) {
        throw new Error(`Cannot convert ${JSON.stringify(raw)} to ${typeName(dataType)}`);
      }
      return value;
    }
    case DataType.String:
      if (["string", "number", "boolean", "bigint"].includes(typeof raw)) return String(raw);
      throw new Error("String values must be a JSON scalar");
    case DataType.DateTime: {
      const value = raw instanceof Date ? raw : new Date(String(raw));
      if (Number.isNaN(value.getTime()))
        throw new Error(`${JSON.stringify(raw)} is not a DateTime`);
      return value;
    }
    case DataType.Guid: {
      const value = String(raw).toLowerCase();
      if (
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)
      ) {
        throw new Error(`${JSON.stringify(raw)} is not a Guid`);
      }
      return value;
    }
    case DataType.ByteString:
      return bytes(raw);
    case DataType.NodeId:
      return coerceNodeId(String(raw));
    case DataType.LocalizedText:
      return typeof raw === "string" ? { text: raw } : raw;
    case DataType.QualifiedName:
      return typeof raw === "string" ? { name: raw } : raw;
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
