// The one numeric-string grammar.
//
// A number can reach a write as a string — "42.5" — and three places read it:
// the write codec, the operator policy's bounds, and the method-argument guess.
// Each used to parse it with its runtime's own number parser, and the two
// runtimes' parsers disagree about almost everything but plain decimals:
// `Number()` takes "0x10", "" and "Infinity", Python's `float()` takes "1_000",
// "inf", "nan" and Arabic-Indic digits. So the same string could be written to
// the plant by one runtime, refused by the other, or pass a bound on one and not
// the other (#157).
//
// Now there is one grammar, JSON's own number grammar — the syntax a number
// would have had if it had not been quoted — with surrounding JSON whitespace
// allowed. `numeric.py` is the other half, and tests/fixtures/write-coercion.json
// and value-bounds.json pin both to the same table.

/** JSON's number grammar (RFC 8259 §6). */
const JSON_NUMBER = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;
const JSON_NUMBER_PARTS = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/;

/** The whitespace JSON allows around a value. Not `trim()`, whose idea of
 * whitespace (NBSP, U+2028, …) differs from Python's `str.strip()`. */
const JSON_WHITESPACE = /^[ \t\n\r]+|[ \t\n\r]+$/g;

/** `text` without surrounding JSON whitespace, if it is a JSON number. */
export function numericText(text: string): string | null {
  const trimmed = text.replace(JSON_WHITESPACE, "");
  return JSON_NUMBER.test(trimmed) ? trimmed : null;
}

/** A numeric string's value, or null if it is not one.
 *
 * null for anything outside the grammar, and for a number too large to be a
 * finite double ("1e400") — there is no value to compare or write.
 */
export function parseNumericString(text: string): number | null {
  const trimmed = numericText(text);
  if (trimmed === null) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/** Past this many decimal digits no integer type OPC UA has can hold the value,
 * so an exponent is not expanded further ("1e999999999" must not allocate). */
const MAX_INTEGER_DIGITS = 40;

/** A JSON-number string's exact integer value.
 *
 * "5.0" and "1e3" are integers and "1.5" is not (null). Evaluated on the
 * digits, not through a double, so a 64-bit value survives whole. "too large"
 * when the value has more digits than any integer type holds.
 */
export function exactInteger(text: string): bigint | "too large" | null {
  const match = JSON_NUMBER_PARTS.exec(text);
  if (!match) return null;
  const [, sign, whole, fraction = "", exponent = "0"] = match;
  let digits = (whole + fraction).replace(/^0+/, "");
  if (digits === "") return 0n;
  // An exponent is at most a few digits in anything real; a long one is
  // compared as a BigInt so it cannot lose precision on the way to "too large".
  let scale = BigInt(exponent) - BigInt(fraction.length);
  if (scale < 0n) {
    if (BigInt(digits.length) <= -scale) {
      return null; // every digit is fractional, and at least one is not zero
    }
    const cut = digits.length + Number(scale);
    if (/[^0]/.test(digits.slice(cut))) return null;
    digits = digits.slice(0, cut);
    scale = 0n;
  }
  if (BigInt(digits.length) + scale > BigInt(MAX_INTEGER_DIGITS)) return "too large";
  const value = BigInt(digits) * 10n ** scale;
  return sign ? -value : value;
}
