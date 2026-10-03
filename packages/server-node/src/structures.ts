/** Namespace-zero ExtensionObject Body fields, never library debug strings (#171). */
import { DataType } from "node-opcua-client";
import { CONTRACT } from "./contract.js";

type Field = { name: string; originalName?: string; fieldType: string; isArray?: boolean };
type Schema = {
  dataTypeNodeId?: { namespace: number; value?: number | string };
  fields: Field[];
  getBaseSchema?: () => Schema | null;
};
type Structured = Record<string, unknown> & { schema?: Schema };

export function extensionToJson(
  value: unknown,
  scalar: (value: unknown, type: DataType | undefined) => unknown
): Record<string, unknown> {
  const unavailable = () => {
    throw new Error("undecodableExtensionObject");
  };
  function field(item: any, kind: string, depth: number): unknown {
    if (depth > CONTRACT.limits.maxNestingDepth) return unavailable();
    if (item == null) return null;
    if (kind === "LocalizedText") return { Locale: item.locale ?? null, Text: item.text ?? null };
    if (kind === "QualifiedName") return { NamespaceIndex: item.namespaceIndex, Name: item.name };
    if (kind === "Variant") {
      const kind = DataType[item.dataType];
      if (item.arrayType) {
        if (item.value?.length > CONTRACT.limits.maxArrayItems) return unavailable();
        return item.value == null
          ? null
          : Array.from(item.value as ArrayLike<unknown>, (x) => field(x, kind, depth + 1));
      }
      return field(item.value, kind, depth + 1);
    }
    if (kind === "ExtensionObject" || item.schema?.fields) return structure(item, depth + 1);
    return scalar(item, DataType[kind as keyof typeof DataType] as DataType | undefined);
  }
  function structure(item: Structured, depth: number): Record<string, unknown> {
    if (
      depth > CONTRACT.limits.maxNestingDepth ||
      item.schema?.dataTypeNodeId?.namespace !== 0 ||
      item.schema?.dataTypeNodeId?.value === 22 ||
      item.schema?.dataTypeNodeId?.value === 0
    ) {
      return unavailable();
    }
    let schema: Schema | null = item.schema;
    const schemas: Schema[] = [];
    while (schema) {
      if (schemas.includes(schema) || schemas.length > CONTRACT.limits.maxNestingDepth)
        return unavailable();
      schemas.unshift(schema);
      schema = schema.getBaseSchema?.() ?? null;
    }
    const entries: [string, unknown][] = [];
    for (const current of schemas) {
      for (const f of current.fields) {
        if (entries.length >= CONTRACT.limits.maxArrayItems) return unavailable();
        const raw = item[f.name];
        let encoded;
        if (f.isArray && raw != null) {
          const values = raw as ArrayLike<unknown>;
          if (values.length > CONTRACT.limits.maxArrayItems) return unavailable();
          encoded = Array.from(values, (x) => field(x, f.fieldType, depth + 1));
        } else {
          encoded = field(raw, f.fieldType, depth + 1);
        }
        entries.push([f.originalName ?? f.name, encoded]);
      }
    }
    return Object.fromEntries(entries);
  }
  try {
    return structure(value as Structured, 0);
  } catch {
    return { $opcua: "undecodableExtensionObject" };
  }
}
