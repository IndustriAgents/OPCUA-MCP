// What a node's declared DataType is written as, driven by the table both runtimes
// share.
//
// `tests/unit/test_data_type_resolution.py` reads the same
// `tests/fixtures/data-type-resolution.json`. The write path converts a value to
// what this answers, so a type one runtime resolves and the other does not is a
// write converted two ways — or sent by one and inferred from the current value
// by the other.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { resolveDataType } from "../build/node-facts.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "data-type-resolution.json"), "utf8")
);

describe("DataType resolution, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, async () => {
      const resolved = await resolveDataType(
        testCase.data_type_id,
        (child) => testCase.supertypes[child] ?? null
      );
      assert.deepEqual(resolved, testCase.expect);
    });
  }

  it("treats a lookup that throws as unresolved, never as an error", async () => {
    const resolved = await resolveDataType("ns=2;i=7", async () => {
      throw new Error("BadTimeout");
    });
    assert.deepEqual(resolved, { data_type: null, enumeration: false });
  });

  it("reads the short spelling of a namespace-0 id as the long one", async () => {
    assert.deepEqual(await resolveDataType("i=11", () => null), {
      data_type: "Double",
      enumeration: false,
    });
  });
});
