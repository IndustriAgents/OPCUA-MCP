// What the policy check on connect finds, driven by the table both runtimes share.
//
// `tests/unit/test_policy_check.py` reads the same
// `tests/fixtures/policy-check.json`. The input is what resolving a policy against
// a live server produced, so the rules are pinned here and the resolving is pinned
// by `policy-resolution.test.mjs` and end to end.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { policyFindings } from "../build/policy-check.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "policy-check.json"), "utf8")
);

describe("the policy check, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      assert.deepEqual(policyFindings(testCase.input), testCase.expect);
    });
  }
});
