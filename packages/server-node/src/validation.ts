/** Check a call's arguments against the contract's own input schema.
 *
 * `validation.py` is the Python half and must accept and reject exactly the same
 * calls, because the schema being checked is the same document.
 *
 * This is deliberately *not* a JSON Schema implementation. The contract uses six
 * keywords — `type`, `items`, `properties`, `required`, `minItems` and nothing
 * else — and a validator that covers only those is one that can be read in full
 * and mirrored in another language without a dependency on either side. A
 * keyword appearing in the contract that is not handled here would be silently
 * ignored, so the supported set is asserted against the contract by
 * tests/unit/test_contract.py.
 *
 * Why check at all: this runtime had no validation whatsoever. The low-level MCP
 * `Server` does not check `arguments` against the advertised `inputSchema`, and
 * `dispatch` cast straight off the wire (`args.node_ids as string[]`), so a
 * malformed call reached node-opcua as whatever the client sent. The Python
 * runtime validated against a *different* schema — the one MCPServer derives
 * from the function signature — which knows that `nodes` is a list of objects
 * and nothing about what belongs in one. Both now answer to the contract, and
 * word the refusal identically.
 */

import { message } from "./errors.js";

/** How each declared type is named in a refusal. */
const TYPE_NAMES: Record<string, string> = {
  string: "a string",
  integer: "an integer",
  number: "a number",
  boolean: "a boolean",
  object: "an object",
  array: "an array",
};

/** Whether `value` is of the declared JSON type.
 *
 * `integer` accepts any whole number, which is all JavaScript can offer — and so
 * is what the Python half must accept too, hence its whole-valued-float rule.
 */
function matches(value: unknown, declared: string): boolean {
  switch (declared) {
    case "boolean":
      return typeof value === "boolean";
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "string":
      return typeof value === "string";
    case "array":
      return Array.isArray(value);
    case "object":
      return typeof value === "object" && value !== null && !Array.isArray(value);
    default:
      // A type the contract declares and this validator does not know. Accepting
      // is the safe direction: the alternative is refusing a call the contract
      // allows.
      return true;
  }
}

/** How a declared type is worded in a refusal: "an array of strings". */
function expected(schema: any): string {
  const declared = schema?.type;
  if (Array.isArray(declared)) {
    return declared.map((entry: string) => TYPE_NAMES[entry] ?? entry).join(" or ");
  }
  if (declared === "array") {
    const itemType = schema?.items?.type;
    if (itemType === "string") return "an array of strings";
    if (itemType === "object") return "an array of objects";
    return "an array";
  }
  return TYPE_NAMES[declared] ?? String(declared);
}

/** Throw if `args` do not satisfy the contract schema for `tool`.
 *
 * The message is a contract one, so the Python runtime refuses the same call
 * with the same sentence.
 */
export function validateArguments(tool: string, schema: any, args: unknown): void {
  if (typeof args !== "object" || args === null || Array.isArray(args)) {
    throw new Error(message("wrongType", { tool, argument: "arguments", expected: "an object" }));
  }
  checkObject(tool, schema, args as Record<string, unknown>, "");
}

function checkObject(
  tool: string,
  schema: any,
  value: Record<string, unknown>,
  prefix: string
): void {
  for (const name of schema?.required ?? []) {
    if (value[name] === undefined || value[name] === null) {
      throw new Error(message("missingArgument", { tool, argument: `${prefix}${name}` }));
    }
  }

  for (const [name, subschema] of Object.entries(schema?.properties ?? {})) {
    // Absent is not wrong: `required` above has already refused the ones that had
    // to be there, and everything else carries a default.
    if (value[name] === undefined || value[name] === null) continue;
    checkValue(tool, subschema, value[name], `${prefix}${name}`);
  }
}

function checkValue(tool: string, schema: any, value: unknown, path: string): void {
  const declared = schema?.type;
  // `value` in a write entry deliberately declares no type: what may be written
  // is decided by the target node, not by this schema.
  if (declared === undefined) return;

  if (typeof declared === "string" && !matches(value, declared)) {
    throw new Error(message("wrongType", { tool, argument: path, expected: expected(schema) }));
  }

  if (declared === "array") {
    const list = value as unknown[];
    if (schema.minItems !== undefined && list.length < schema.minItems) {
      // The one rule with its own sentence, because it is the one a model trips
      // over: an empty list is a well-formed array and a meaningless request,
      // and "must be a non-empty array" says what to do about it.
      throw new Error(message("emptyArray", { tool, argument: path }));
    }
    // `items: {}` is truthy here and falsy in Python; compare against undefined
    // on both sides so the two halves take the same route, not merely reach the
    // same answer.
    if (schema.items !== undefined) {
      list.forEach((element, index) =>
        checkValue(tool, schema.items, element, `${path}[${index}]`)
      );
    }
  } else if (declared === "object") {
    checkObject(tool, schema, value as Record<string, unknown>, `${path}.`);
  }
}
