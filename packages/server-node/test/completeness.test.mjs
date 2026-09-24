// What `completeness` says for each way a result can come back short (#137),
// driven by the table both runtimes share.
//
// `tests/unit/test_completeness.py` reads the same
// `tests/fixtures/completeness.json` and asserts the same objects, field for
// field — so the two runtimes cannot agree on `complete` while disagreeing about
// `remaining`.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  bufferCompleteness,
  drainCompleteness,
  historyCompleteness,
  traversalCompleteness,
} from "../build/completeness.js";
import { CONTRACT } from "../build/contract.js";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const table = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests", "fixtures", "completeness.json"), "utf8")
);
const MAX = {
  history: CONTRACT.limits.maxHistoryValues,
  traversal: CONTRACT.traversal.maxNodes,
};

/** A table entry, with "max" standing for the cap of the table it is in. */
function resolve(value, cap) {
  if (value === "max") return cap;
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, resolve(item, cap)])
    );
  }
  return value;
}

describe("completeness", () => {
  for (const testCase of table.history) {
    it(`history: ${testCase.name}`, () => {
      const given = resolve(testCase.input, MAX.history);
      assert.deepEqual(
        historyCompleteness({
          returned: given.returned,
          fetched: given.fetched,
          wanted: given.wanted,
          continuationPoint: given.continuation_point,
          nextStart: given.next_start,
        }),
        resolve(testCase.expected, MAX.history)
      );
    });
  }

  for (const testCase of table.drain) {
    it(`drain: ${testCase.name}`, () => {
      assert.deepEqual(drainCompleteness(testCase.input), testCase.expected);
    });
  }

  for (const testCase of table.traversal) {
    it(`traversal: ${testCase.name}`, () => {
      const given = resolve(testCase.input, MAX.traversal);
      assert.deepEqual(
        traversalCompleteness({
          returned: given.returned,
          truncated: given.truncated,
          maxNodes: given.max_nodes,
          unbrowsable: given.unbrowsable,
        }),
        resolve(testCase.expected, MAX.traversal)
      );
    });
  }

  for (const testCase of table.buffers) {
    it(`buffers: ${testCase.name}`, () => {
      assert.deepEqual(
        bufferCompleteness(testCase.input.dropped.map((dropped) => ({ dropped }))),
        testCase.expected
      );
    });
  }
});
