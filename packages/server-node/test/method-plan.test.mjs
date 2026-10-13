// Whether a method call can work, from the two nodes' own attributes, driven by
// the table both runtimes share.
//
// `tests/unit/test_method_plan.py` reads the same `tests/fixtures/method-plan.json`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { planCall } from "../build/method-plan.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "method-plan.json"), "utf8")
);

describe("method plans, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      assert.equal(planCall(testCase.input), testCase.expect);
    });
  }
});
