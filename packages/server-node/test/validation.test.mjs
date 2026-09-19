// The contract-schema validator, driven by the table both runtimes share.
//
// `tests/unit/test_validation.py` reads the same
// `tests/fixtures/argument-validation.json` and asserts the same verdict and the
// same sentence. A rule that holds there and not here is the divergence this
// file exists to stop — the two servers must refuse the same calls, and say the
// same thing when they do.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { CONTRACT } from "../build/contract.js";
import { validateArguments } from "../build/validation.js";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests", "fixtures", "argument-validation.json"), "utf8")
);
const specs = new Map(CONTRACT.tools.map((tool) => [tool.name, tool]));

describe("contract argument validation", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      const spec = specs.get(testCase.tool);
      assert.ok(spec, `no such tool in the contract: ${testCase.tool}`);
      const run = () => validateArguments(testCase.tool, spec.inputSchema, testCase.arguments);

      if (testCase.error === null) {
        assert.doesNotThrow(run);
        return;
      }
      assert.throws(run, (error) => {
        assert.equal(error.message, testCase.error);
        return true;
      });
    });
  }

  it("refuses arguments that are not an object", () => {
    assert.throws(
      () => validateArguments("read_opcua_nodes", specs.get("read_opcua_nodes").inputSchema, ["a"]),
      { message: "read_opcua_nodes argument arguments must be an object" }
    );
  });
});
