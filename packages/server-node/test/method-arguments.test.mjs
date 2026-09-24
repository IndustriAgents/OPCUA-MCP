// What type a method argument is sent as (#157).
//
// The shared table is tests/fixtures/method-arguments.json;
// tests/unit/test_method_arguments.py drives the same one through the Python
// server. The supertype walk is given the table's hierarchy in place of a live
// server's inverse HasSubtype browse.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DataType } from "node-opcua-client";

import { builtInType, guessVariant } from "../build/method-arguments.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "method-arguments.json"), "utf8")
);
const supertypeOf = async (dataType) => FIXTURE.supertypes[dataType] ?? null;

describe("a declared type resolves to its built-in base", () => {
  for (const testCase of FIXTURE.declared) {
    it(testCase.name, async () => {
      if ("error" in testCase) {
        await assert.rejects(builtInType(testCase.data_type, supertypeOf), (error) => {
          assert.equal(error.message, testCase.error);
          return true;
        });
      } else {
        const dataType = await builtInType(testCase.data_type, supertypeOf);
        assert.equal(DataType[dataType], testCase.expected);
      }
    });
  }
});

describe("an undeclared argument is guessed the same way", () => {
  for (const testCase of FIXTURE.guessed) {
    it(testCase.name, () => {
      if ("error" in testCase) {
        assert.throws(
          () => guessVariant(testCase.value, 0),
          (error) => {
            assert.equal(error.message, testCase.error);
            return true;
          }
        );
      } else {
        const variant = guessVariant(testCase.value, 0);
        assert.equal(DataType[variant.dataType], testCase.expected.type);
        assert.equal(variant.value, testCase.expected.value);
      }
    });
  }
});
