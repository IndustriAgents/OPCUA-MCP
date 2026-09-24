// The write codec: what a JSON value becomes on the wire, per OPC UA type.
//
// The rules are the shared table in tests/fixtures/write-coercion.json, which
// tests/unit/test_variant_codec.py drives through the Python server's codec too
// — refusal messages included, because a write that one runtime sends and the
// other refuses is a different plant depending on which package was installed
// (#157).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DataType, VariantArrayType } from "node-opcua-client";

import { asNumber } from "../build/policy.js";
import { convertForVariant } from "../build/variant-codec.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "write-coercion.json"), "utf8")
);

/** The converted value in the fixture's runtime-neutral form. */
function normalized(type, value) {
  if (type === "Int64" || type === "UInt64") {
    // node-opcua carries 64-bit integers as a [high, low] pair of 32-bit halves.
    const [high, low] = value;
    let big = (BigInt(high) << 32n) | BigInt(low);
    if (type === "Int64" && big >= 1n << 63n) big -= 1n << 64n;
    return big.toString();
  }
  if (value instanceof Date) return value.toISOString();
  if (Buffer.isBuffer(value)) return value.toString("base64");
  if (type === "LocalizedText") return value.text;
  if (type === "QualifiedName") return value.name;
  return value;
}

function caseId(testCase) {
  return `${testCase.type}${testCase.array ? "[]" : ""} <- ${JSON.stringify(testCase.value)}`;
}

describe("write coercion, from the shared table", () => {
  for (const testCase of FIXTURE.cases) {
    it(caseId(testCase), () => {
      const dataType = DataType[testCase.type];
      const arrayType = testCase.array ? VariantArrayType.Array : VariantArrayType.Scalar;
      if ("error" in testCase) {
        assert.throws(
          () => convertForVariant(testCase.value, dataType, arrayType),
          (error) => {
            assert.equal(error.message, testCase.error);
            return true;
          }
        );
        return;
      }
      const converted = convertForVariant(testCase.value, dataType, arrayType);
      const actual = testCase.array
        ? converted.map((item) => normalized(testCase.type, item))
        : normalized(testCase.type, converted);
      assert.deepEqual(actual, testCase.expected);
    });
  }
});

describe("the policy reads numbers with the same grammar", () => {
  for (const { value, expected } of FIXTURE.numbers) {
    it(`asNumber(${JSON.stringify(value)}) is ${expected}`, () => {
      assert.equal(asNumber(value), expected);
    });
  }
});

describe("Variant write codec", () => {
  it("preserves 64-bit integers as node-opcua high/low pairs", () => {
    assert.deepEqual(
      convertForVariant("18446744073709551615", DataType.UInt64),
      [0xffffffff, 0xffffffff]
    );
    assert.deepEqual(convertForVariant("-1", DataType.Int64), [0xffffffff, 0xffffffff]);
  });

  it("parses DateTime values into a Date", () => {
    const value = convertForVariant("2026-09-17T10:30:00Z", DataType.DateTime);
    assert.ok(value instanceof Date);
    assert.equal(value.toISOString(), "2026-09-17T10:30:00.000Z");
  });
});
