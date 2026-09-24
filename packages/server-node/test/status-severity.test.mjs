// Good *severity* is success, and a Good subcode is reported, not hidden (#157).
//
// The shared table is tests/fixtures/status-severity.json;
// tests/unit/test_status_severity.py drives the same one through the Python
// server. This runtime used to compare against `StatusCodes.Good` exactly, so a
// GoodLocalOverride read came back null and a GoodNoData history range failed.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DataType, DataValue, StatusCodes, Variant } from "node-opcua-client";

import { historyData } from "../build/records.js";
import { isGood } from "../build/status.js";
import { checkMaxChange, toNodeValueRecord } from "../build/tools.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "status-severity.json"), "utf8")
);

function double(value, status) {
  return new DataValue({
    value: new Variant({ dataType: DataType.Double, value }),
    statusCode: StatusCodes[status],
  });
}

describe("severity decides success", () => {
  for (const testCase of FIXTURE.statuses) {
    it(testCase.name, () => {
      const status = StatusCodes[testCase.name];
      assert.equal(status.value, testCase.code);
      assert.equal(isGood(status), testCase.good);
    });
  }
});

describe("a read record keeps a Good subcode's value", () => {
  for (const testCase of FIXTURE.reads) {
    it(testCase.name, () => {
      const record = toNodeValueRecord("ns=2;i=90", double(51.75, testCase.status));
      const picked = Object.fromEntries(
        Object.keys(testCase.record).map((key) => [key, record[key]])
      );
      assert.deepEqual(picked, testCase.record);
    });
  }
});

describe("max_change measures from a Good subcode's value", () => {
  for (const testCase of FIXTURE.maxChange) {
    it(testCase.name, () => {
      const call = () =>
        checkMaxChange(
          "ns=2;i=90",
          testCase.value,
          testCase.limit,
          double(testCase.current, testCase.status)
        );
      if (testCase.error === null) {
        call();
      } else {
        assert.throws(call, (error) => {
          assert.equal(error.message, testCase.error);
          return true;
        });
      }
    });
  }
});

describe("an empty history range is empty, not a failure", () => {
  for (const testCase of FIXTURE.history) {
    it(testCase.name, () => {
      const result = { statusCode: StatusCodes[testCase.status], historyData: null };
      if ("error" in testCase) {
        assert.throws(
          () => historyData(result, "Read history", "dataValues"),
          (error) => {
            assert.equal(error.message, testCase.error);
            return true;
          }
        );
      } else {
        assert.deepEqual(historyData(result, "Read history", "dataValues"), testCase.records);
      }
    });
  }
});
