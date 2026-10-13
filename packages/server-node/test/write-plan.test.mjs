// The plan each write gets from its node's own attributes, driven by the table
// both runtimes share.
//
// `tests/unit/test_write_plan.py` reads the same `tests/fixtures/write-plan.json`.
// A write one runtime sends and the other skips is a plant that moved or did not
// depending on which server the operator happened to start.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { planWrite } from "../build/write-plan.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "write-plan.json"), "utf8")
);

describe("write plans, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      const plan = planWrite(
        testCase.request,
        testCase.facts,
        testCase.engineering,
        testCase.options
      );
      assert.deepEqual(plan, testCase.expect);
    });
  }
});
