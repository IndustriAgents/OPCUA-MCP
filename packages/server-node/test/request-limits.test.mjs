// What a request may carry before it reaches the OPC UA server (issue #139),
// driven by the table both runtimes share.
//
// `tests/unit/test_request_limits.py` reads the same
// `tests/fixtures/request-limits.json` and asserts the same verdict and the same
// sentence. Each case runs through everything that happens before the network,
// in the order `callTool` runs it: the request-wide bounds, then the contract
// schema whose `maxItems` carries the per-tool counts.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DataType, VariantArrayType } from "node-opcua-client";

import { CONTRACT } from "../build/contract.js";
import { checkRequestBounds, eventBufferSize } from "../build/limits.js";
import { validateArguments } from "../build/validation.js";
import { convertForVariant } from "../build/variant-codec.js";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const table = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests", "fixtures", "request-limits.json"), "utf8")
);
const specs = new Map(CONTRACT.tools.map((tool) => [tool.name, tool]));

/** Build what a directive describes; `test_request_limits.py` mirrors this. */
function expand(value) {
  if (Array.isArray(value)) return value.map(expand);
  if (value === null || typeof value !== "object") return value;
  const keys = Object.keys(value);
  if (keys.length === 1 && keys[0] === "$string") {
    const spec = value.$string;
    const [count, character] = typeof spec === "number" ? [spec, "a"] : spec;
    return character.repeat(count);
  }
  if (keys.length === 1 && keys[0] === "$array") {
    const [item, count] = value.$array;
    return Array.from({ length: count }, () => expand(item));
  }
  if (keys.length === 1 && keys[0] === "$nest") {
    const [depth, leaf] = value.$nest;
    let built = expand(leaf);
    for (let level = 0; level < depth; level += 1) built = [built];
    return built;
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)]));
}

/** What `callTool` runs before authorization, in its order. */
function precheck(tool, args) {
  checkRequestBounds(tool, args);
  validateArguments(tool, specs.get(tool).inputSchema, args);
}

describe("request limits", () => {
  it("was written against the contract's limits", () => {
    for (const [name, value] of Object.entries(table.limits)) {
      assert.equal(CONTRACT.limits[name], value, `limits.${name} changed; update the table`);
    }
  });

  for (const testCase of table.requests) {
    it(testCase.name, () => {
      const args = expand(testCase.arguments);
      const run = () => precheck(testCase.tool, args);
      if (testCase.error === null) {
        assert.doesNotThrow(run);
        return;
      }
      assert.throws(run, (error) => {
        assert.equal(error.message, testCase.error);
        return true;
      });
    });
  }

  for (const testCase of table.byteStrings) {
    it(testCase.name, () => {
      const text = Buffer.alloc(testCase.bytes).toString("base64");
      const run = () => convertForVariant(text, DataType.ByteString, VariantArrayType.Scalar);
      if (testCase.error === null) {
        assert.equal(run().length, testCase.bytes);
        return;
      }
      assert.throws(run, (error) => {
        assert.equal(error.message, testCase.error);
        return true;
      });
    });
  }

  for (const testCase of table.eventBufferSizes) {
    it(`event buffer: ${testCase.name}`, () => {
      assert.equal(eventBufferSize(testCase.requested), testCase.applied);
    });
  }
});
