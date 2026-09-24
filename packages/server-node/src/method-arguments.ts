// What type a method argument is sent as.
//
// Two questions, and the two runtimes used to answer both differently (#157):
//
// - The method declares an argument's DataType, but it is not a built-in one —
//   `Duration` (i=290), `UtcTime` (i=294), an enumeration. This runtime failed
//   the call; the Python one could not look the type up, fell back to *guessing
//   every argument*, and sent the call. Now both walk the DataType's supertypes
//   to the built-in type it is encoded as — Part 3 §5.8.2: a Duration *is* a
//   Double on the wire, an enumeration an Int32 — and refuse, before anything is
//   sent, when there is none.
// - The method declares nothing. Then a JSON boolean, number or string maps to
//   Boolean, Double or String — the number as a Double because JSON cannot tell
//   5 from 5.0 — and a numeric string is a Double when it is a number by the
//   shared grammar. A null, array or object has no single obvious OPC UA type,
//   and the two runtimes had picked different ones ("1,2" against an Int64
//   array), so it is refused.
//
// `method_arguments.py` is the other half; tests/fixtures/method-arguments.json
// holds both to the same table.
import { DataType, Variant } from "node-opcua-client";

import { numericText } from "./numeric.js";
import { convertForVariant } from "./variant-codec.js";

/** Enumeration: every enumerated DataType is encoded as an Int32 (Part 6 §5.2.4). */
const ENUMERATION = 29;
/** Far deeper than any real type hierarchy; bounds a server whose HasSubtype
 * references form a loop. */
const MAX_DEPTH = 32;

const NS0_NUMERIC = /^ns=0;i=([0-9]+)$/;

/** The built-in type `dataType` is encoded as, walking up its supertypes.
 *
 * `dataType` is a canonical node id (`ns=0;i=290`); `supertypeOf` gives the
 * parent of one, or null. Throws when the walk ends without reaching a built-in
 * type — the argument cannot be encoded, and sending a guess instead is exactly
 * what this exists to stop.
 */
export async function builtInType(
  dataType: string,
  supertypeOf: (dataType: string) => Promise<string | null>
): Promise<DataType> {
  let current: string | null = dataType;
  for (let depth = 0; depth < MAX_DEPTH && current !== null; depth++) {
    const match = NS0_NUMERIC.exec(current);
    if (match) {
      // ns=0 identifiers 1-25 *are* the built-in types (Part 6 §5.1.2),
      // numbered the same as the DataType enum.
      const identifier = Number(match[1]);
      if (identifier >= 1 && identifier <= 25) return identifier as DataType;
      if (identifier === ENUMERATION) return DataType.Int32;
    }
    current = await supertypeOf(current);
  }
  throw new Error(
    `DataType ${dataType} is not a subtype of any built-in OPC UA type, so an ` +
      "argument of it cannot be encoded; nothing was sent"
  );
}

function kind(value: unknown): string {
  if (value === null || value === undefined) return "null";
  return Array.isArray(value) ? "an array" : "an object";
}

/** An argument for a method that declares no InputArguments. */
export function guessVariant(value: unknown, index: number): Variant {
  if (typeof value === "boolean") return new Variant({ dataType: DataType.Boolean, value });
  if (typeof value === "number" || (typeof value === "string" && numericText(value) !== null)) {
    return new Variant({
      dataType: DataType.Double,
      value: convertForVariant(value, DataType.Double),
    });
  }
  if (typeof value === "string") return new Variant({ dataType: DataType.String, value });
  throw new Error(
    `arguments[${index}] is ${kind(value)}, and the method publishes no ` +
      "InputArguments to say what type it expects; pass a boolean, a number or a string"
  );
}
