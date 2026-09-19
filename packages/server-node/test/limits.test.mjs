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

import { MAX_HISTORY_VALUES, historyValues, historyWasClipped } from "../build/limits.js";

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

  for (const testCase of table.clipping) {
    it(testCase.name, () => {
      assert.equal(
        historyWasClipped(number(testCase.returned), number(testCase.wanted)),
        testCase.clipped
      );
    });
  }
});
