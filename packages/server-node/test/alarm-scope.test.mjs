// The operator policy's alarm scope, driven by the table both runtimes share.
//
// `tests/unit/test_alarm_scope.py` reads the same `tests/fixtures/alarm-scope.json`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { checkAlarmScope } from "../build/policy-check.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "alarm-scope.json"), "utf8")
);

describe("alarm scope, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      assert.equal(checkAlarmScope(testCase.input), testCase.expect);
    });
  }
});
