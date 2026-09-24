// What a malformed OPCUA_POLICY_FILE is told, driven by the table both runtimes
// share (#157).
//
// `tests/unit/test_policy_file_validation.py` reads the same
// `tests/fixtures/policy-file-validation.json` and asserts the same verdict and
// the same sentence. Before it existed each runtime did whatever its own code
// happened to do with a surprise: a traceback on one, a silent
// `undefined|undefined` allowlist entry on the other, and on both a flag of
// `"false"` that switched insecure control *on*.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { parsePolicyConfig } from "../build/policy.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "policy-file-validation.json"), "utf8")
);

/** One case's file on disk, exactly as the table spells it. */
function writeCase(testCase) {
  const path = join(mkdtempSync(join(tmpdir(), "opcua-policy-file-")), "policy.json");
  const text = testCase.text ?? JSON.stringify(testCase.file);
  writeFileSync(path, text, "utf8");
  return path;
}

/** The settings `expect` can name, spelled as the table spells them. */
function settings(config) {
  const sorted = (set) => [...set].sort();
  return {
    profile: config.profile,
    allowed_tools: config.allowedTools === null ? null : sorted(config.allowedTools),
    writable_nodes: sorted(config.writableNodes),
    callable_methods: sorted(config.callableMethods),
    acknowledge_alarms: config.acknowledgeAlarms,
    allow_insecure_control: config.allowInsecureControl,
    allow_out_of_range_writes: config.allowOutOfRangeWrites,
  };
}

describe("policy file validation, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      const path = writeCase(testCase);
      const parse = () => parsePolicyConfig({ OPCUA_POLICY_FILE: path, ...(testCase.env ?? {}) });

      if (testCase.error === null) {
        const actual = settings(parse());
        for (const [key, expected] of Object.entries(testCase.expect ?? {})) {
          assert.deepEqual(actual[key], expected, key);
        }
        return;
      }
      assert.throws(parse, (error) => {
        if (testCase.errorPrefix !== undefined) {
          assert.ok(
            error.message.startsWith(testCase.errorPrefix.replace("{path}", path)),
            error.message
          );
        } else {
          assert.equal(error.message, testCase.error.replace("{path}", path));
        }
        return true;
      });
    });
  }
});
