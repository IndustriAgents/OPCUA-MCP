import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DataType, VariantArrayType } from "node-opcua-client";

import { convertForVariant } from "../build/variant-codec.js";

describe("Variant write codec", () => {
  it("parses booleans explicitly", () => {
    assert.equal(convertForVariant(false, DataType.Boolean), false);
    assert.equal(convertForVariant("false", DataType.Boolean), false);
    assert.equal(convertForVariant("yes", DataType.Boolean), true);
    assert.throws(() => convertForVariant("definitely", DataType.Boolean), /Boolean/);
  });

  it("range-checks integral OPC UA types", () => {
    assert.equal(convertForVariant("-2147483648", DataType.Int32), -2147483648);
    assert.throws(() => convertForVariant("1.5", DataType.Int32), /Int32/);
    assert.throws(() => convertForVariant("2147483648", DataType.Int32), /range/);
  });

  it("preserves 64-bit integers as node-opcua high/low pairs", () => {
    assert.deepEqual(
      convertForVariant("18446744073709551615", DataType.UInt64),
      [0xffffffff, 0xffffffff]
    );
    assert.deepEqual(convertForVariant("-1", DataType.Int64), [0xffffffff, 0xffffffff]);
  });

  it("decodes ByteString base64", () => {
    assert.deepEqual(convertForVariant("AP8=", DataType.ByteString), Buffer.from([0, 255]));
    assert.throws(() => convertForVariant("not base64!", DataType.ByteString), /base64/);
  });

  it("parses and converts arrays", () => {
    assert.deepEqual(
      convertForVariant('["1", "2"]', DataType.Int16, VariantArrayType.Array),
      [1, 2]
    );
  });

  it("parses DateTime values", () => {
    const value = convertForVariant("2026-09-17T10:30:00Z", DataType.DateTime);
    assert.ok(value instanceof Date);
    assert.equal(value.toISOString(), "2026-09-17T10:30:00.000Z");
  });
});
