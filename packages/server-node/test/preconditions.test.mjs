// The operator policy's interlocks, driven by the table both runtimes share.
//
// `tests/unit/test_preconditions.py` reads the same
// `tests/fixtures/preconditions.json`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { checkPreconditions } from "../build/policy-check.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "preconditions.json"), "utf8")
);

describe("preconditions, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      assert.equal(checkPreconditions(testCase.input), testCase.expect);
    });
  }
});
