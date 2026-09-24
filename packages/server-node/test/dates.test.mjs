// The date/time grammar, from the shared table (#157).
//
// Every string a tool accepts as a time — history start_time/end_time, and a
// DateTime write — goes through `toDate`. The two runtimes used to disagree
// about both the grammar (this one used `new Date()`, which reads
// "April 23, 2026" and rolls "2026-02-30" over to March) and about what a value
// with no zone means (the host's local time here, UTC there).
// tests/unit/test_datetime_parsing.py drives the same table through the Python
// server's `parse_iso_datetime`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { toDate } from "../build/dates.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "datetime-parsing.json"), "utf8")
);

describe("date/time parsing, from the shared table", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      if ("error" in testCase) {
        assert.throws(
          () => toDate(testCase.input),
          (error) => {
            assert.equal(error.message, testCase.error);
            return true;
          }
        );
      } else {
        assert.equal(toDate(testCase.input).toISOString(), testCase.expected);
      }
    });
  }
});
