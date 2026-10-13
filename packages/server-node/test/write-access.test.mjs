// What read_opcua_nodes reports as write_access, driven by the table both
// runtimes share.
//
// `tests/unit/test_write_access.py` reads the same
// `tests/fixtures/write-access.json`, and asserts the record key for key: an
// agent reads this to decide what to write, and two runtimes that described one
// node two ways would teach it two sets of rules.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { CONTRACT } from "../build/contract.js";
import { writeAccess } from "../build/write-plan.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "write-access.json"), "utf8")
);
const SHAPE = CONTRACT.resultShapes.nodeValues.items.properties.write_access;

describe("write access, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      const record = writeAccess(testCase.input);
      assert.deepEqual(record, testCase.expect);
      // Every key the contract requires, and no other.
      assert.deepEqual(Object.keys(record).sort(), [...SHAPE.required].sort());
    });
  }
});
