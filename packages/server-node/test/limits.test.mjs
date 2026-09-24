// How num_values resolves, driven by the table both runtimes share.
//
// `tests/unit/test_limits.py` reads the same `tests/fixtures/history-limits.json`.
// A cap that holds there and not here is two servers disagreeing about how much
// "as much as allowed" is, which is the divergence the shared `limits` block in
// the contract exists to stop.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { historyCompleteness } from "../build/completeness.js";
import { MAX_HISTORY_VALUES, historyValues } from "../build/limits.js";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const table = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests", "fixtures", "history-limits.json"), "utf8")
);

/** A table entry, with "max" standing for the contract's own cap. */
const number = (value) => (value === "max" ? MAX_HISTORY_VALUES : value);

describe("history limits", () => {
  for (const testCase of table.cases) {
    it(testCase.name, () => {
      assert.equal(historyValues(number(testCase.num_values)), number(testCase.wanted));
    });
  }

  // The notice fires on `contractLimit`, and only on it. Driven through the
  // completeness builder since #137, because that is now where the decision is
  // made; the table and its verdicts are unchanged.
  for (const testCase of table.clipping) {
    it(testCase.name, () => {
      const completeness = historyCompleteness({
        returned: number(testCase.returned),
        fetched: number(testCase.returned),
        wanted: number(testCase.wanted),
        continuationPoint: false,
        nextStart: null,
      });
      assert.equal(completeness.reasons.includes("contractLimit"), testCase.clipped);
    });
  }
});
