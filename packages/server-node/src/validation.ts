/** Draft 2020-12 validity comes from Ajv; only error ordering/wording lives here. */
import { Ajv2020 } from "ajv/dist/2020.js";
import type { ErrorObject, AnySchema, ValidateFunction } from "ajv";
import { message } from "./errors.js";

const ajv = new Ajv2020({
  strict: true,
  strictTypes: false,
  allErrors: true,
  allowUnionTypes: true,
  ownProperties: true,
});
const compiled = new WeakMap<object, ValidateFunction>();
const TYPE_NAMES: Record<string, string> = {
  string: "a string",
  integer: "an integer",
  number: "a number",
  boolean: "a boolean",
  object: "an object",
  array: "an array",
  null: "null",
};
export type Schema = {
  type?: string | string[];
  items?: Schema;
  properties?: Record<string, Schema>;
  required?: string[];
  enum?: unknown[];
  minimum?: number;
  minItems?: number;
  maxItems?: number;
  additionalProperties?: boolean | Schema;
  [key: string]: unknown;
};
function expected(schema: Schema): string {
  if (Array.isArray(schema.type)) {
    const nonNull = schema.type.filter((t) => t !== "null");
    if (nonNull.length === 1) return expected({ ...schema, type: nonNull[0] });
    return schema.type
      .filter((t) => t !== "null")
      .map((t) => TYPE_NAMES[t] ?? t)
      .join(" or ");
  }
  if (schema.type === "array") {
    if (schema.items?.type === "string") return "an array of strings";
    if (schema.items?.type === "object") return "an array of objects";
  }
  return TYPE_NAMES[String(schema.type)] ?? String(schema.type);
}
function locate(schema: Schema, args: unknown, pointer: string) {
  const parts = pointer
    ? pointer
        .slice(1)
        .split("/")
        .map((p) => p.replace(/~1/g, "/").replace(/~0/g, "~"))
    : [];
  let value = args,
    parent = schema,
    path = "";
  const order: number[] = [];
  for (const part of parts) {
    parent = schema;
    if (Array.isArray(value)) {
      order.push(6, Number(part));
      path += `[${part}]`;
      schema = schema.items ?? {};
      value = value[Number(part)];
    } else {
      const keys = Object.keys(schema.properties ?? {});
      order.push(6, keys.indexOf(part));
      path += (path ? "." : "") + part;
      schema = schema.properties?.[part] ?? {};
      value = (value as Record<string, unknown>)[part];
    }
  }
  return { schema, parent, value, path, order, parts };
}
export class ValidationRefusal extends Error {
  constructor(
    readonly code: string,
    readonly argument: string,
    text: string
  ) {
    super(text);
  }
}
function normalize(tool: string, root: Schema, args: unknown, error: ErrorObject) {
  const { schema, parent, value, path, order, parts } = locate(root, args, error.instancePath);
  const params: Record<string, string | number> = { tool, argument: path };
  let code = "schemaConstraint",
    rank = 3;
  const keyword = error.keyword;
  if (keyword === "type") {
    code = "wrongType";
    rank = 0;
    if (value === null && parts.length && parent.required?.includes(parts.at(-1)!)) {
      code = "missingArgument";
      rank = 5;
      order.splice(-2);
    } else params.expected = expected(schema);
  } else if (keyword === "required") {
    code = "missingArgument";
    rank = 5;
    params.argument = path + (path ? "." : "") + error.params.missingProperty;
  } else if (keyword === "additionalProperties") {
    code = "unknownArgument";
    rank = 4;
    params.argument = path + (path ? "." : "") + error.params.additionalProperty;
    params.allowed = Object.keys(schema.properties ?? {}).join(", ") || "no arguments";
  } else if (keyword === "enum") {
    code = "notAllowedValue";
    rank = 1;
    params.allowed = (schema.enum ?? [])
      .filter((v) => v !== null)
      .map((v) => JSON.stringify(v))
      .join(", ");
    params.value = JSON.stringify(value) ?? "undefined";
  } else if (keyword === "minimum") {
    code = "belowMinimum";
    rank = 2;
    params.minimum = schema.minimum!;
    params.value = JSON.stringify(value) ?? "undefined";
  } else if (keyword === "minItems" && schema.minItems === 1) code = "emptyArray";
  else if (keyword === "maxItems") {
    code = "tooManyItems";
    params.limit = schema.maxItems!;
    params.count = (value as unknown[]).length;
  }
  if (keyword === "uniqueItems") rank = 7;
  if (code === "schemaConstraint") {
    params.constraint = keyword;
    if (!path) params.argument = "arguments";
  }
  if (code === "missingArgument") {
    const requiredSchema = keyword === "type" ? parent : schema;
    const requiredName = keyword === "type" ? parts.at(-1)! : error.params.missingProperty;
    order.push(rank, requiredSchema.required!.indexOf(requiredName));
  } else order.push(rank);
  return { order, code, params };
}
function compare(a: number[], b: number[]) {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}
export function validateArguments(tool: string, schema: Schema, args: unknown): void {
  if (typeof args !== "object" || args === null || Array.isArray(args))
    throw new ValidationRefusal(
      "wrongType",
      "arguments",
      message("wrongType", { tool, argument: "arguments", expected: "an object" })
    );
  let validate = compiled.get(schema);
  if (!validate) {
    validate = ajv.compile(schema as AnySchema);
    compiled.set(schema, validate);
  }
  if (validate(args)) return;
  const first = (validate.errors ?? [])
    .map((e) => normalize(tool, schema, args, e))
    .sort((a, b) => compare(a.order, b.order))[0];
  if (first)
    throw new ValidationRefusal(
      first.code,
      String(first.params.argument),
      message(first.code, first.params)
    );
}
