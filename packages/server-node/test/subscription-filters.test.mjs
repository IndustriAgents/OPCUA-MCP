// What a subscription reports, beyond how often it looks (issue #118).
//
// `subscribe_opcua_nodes` took `publishing_interval`, `sampling_interval` and
// `buffer_size` — and no filter. Point it at a noisy analogue tag and the default
// 20-record ring fills with sensor jitter in about a second: the agent reads it
// back, sees nothing but noise, and has spent one of the server's 200
// subscriptions to get it.
//
// OPC UA's answer is `DataChangeFilter` (Part 4 §7.22), and it is better than
// filtering after the fact because the values never leave the server — no
// bandwidth, no buffer, no round trip.
//
// `tests/unit/test_subscription_filters.py` is the Python half and drives the
// same shared table. What is *not* here is the percent deadband's EURange
// requirement, which needs a real node; that is in
// `tests/e2e/test_engineering_units_e2e.py`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DataChangeTrigger, DeadbandType } from "node-opcua-client";

import {
  DATA_CHANGE_TRIGGERS,
  DEADBAND_TYPES,
  DEFAULT_DATA_CHANGE_TRIGGER,
  DEFAULT_FILTER,
  isDefaultFilter,
  resolveFilter,
} from "../build/subscriptions.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const CASES = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "subscription-filters.json"), "utf8")
).cases;

function call(args) {
  return resolveFilter({
    deadbandType: args.deadband_type,
    deadbandValue: args.deadband_value,
    dataChangeTrigger: args.data_change_trigger,
  });
}

describe("subscription filters, from the shared table", () => {
  for (const testCase of CASES) {
    // The Python half resolves the same request the same way.
    it(testCase.name, () => {
      if (testCase.error !== undefined) {
        assert.throws(
          () => call(testCase.arguments),
          (error) => {
            assert.equal(error.message, testCase.error);
            return true;
          }
        );
        return;
      }
      const resolved = call(testCase.arguments);
      assert.equal(resolved.deadbandType, testCase.filter.deadband_type);
      assert.equal(resolved.deadbandValue, testCase.filter.deadband_value);
      assert.equal(resolved.trigger, testCase.filter.trigger);
    });
  }
});

describe("what the names mean to the library", () => {
  // The contract names them; node-opcua supplies the numbers. Same idea as the
  // dead-session status codes (#112): a name the library stops publishing fails
  // here rather than quietly resolving to undefined. The numbering is fixed by
  // Part 4 §7.22, which is what lets the Python half map the same names onto
  // *its* library and provably agree.
  it("maps every deadband the contract names onto node-opcua's enum", () => {
    assert.deepEqual(DEADBAND_TYPES, {
      none: DeadbandType.None,
      absolute: DeadbandType.Absolute,
      percent: DeadbandType.Percent,
    });
    for (const value of Object.values(DEADBAND_TYPES)) {
      assert.equal(typeof value, "number");
    }
  });

  it("maps every trigger the contract names onto node-opcua's enum", () => {
    assert.deepEqual(DATA_CHANGE_TRIGGERS, {
      status: DataChangeTrigger.Status,
      statusValue: DataChangeTrigger.StatusValue,
      statusValueTimestamp: DataChangeTrigger.StatusValueTimestamp,
    });
  });

  it("does not take OPC UA's own default trigger", () => {
    // OPC UA defaults a DataChangeFilter to `Status`, and that is the wrong
    // default here: an agent that asked to watch a *value* and was told only
    // about status transitions would have been given something nobody asks for.
    assert.equal(DEFAULT_DATA_CHANGE_TRIGGER, "statusValue");
    assert.equal(DataChangeTrigger.Status, 0, "OPC UA's own default, for contrast");
  });
});

describe("the default is still the default", () => {
  // No `DataChangeFilter` is sent at all when none was wanted: a server is
  // entitled to reject a filter it does not implement, and there is no reason to
  // risk that for a subscription that asked for nothing special.
  it("recognises a filter that asks the server for nothing", () => {
    assert.equal(isDefaultFilter(DEFAULT_FILTER), true);
    assert.equal(isDefaultFilter({ ...DEFAULT_FILTER, trigger: "statusValue" }), true);
    assert.equal(
      isDefaultFilter({ deadbandType: "absolute", deadbandValue: 1, trigger: "statusValue" }),
      false
    );
    assert.equal(isDefaultFilter({ ...DEFAULT_FILTER, trigger: "status" }), false);
  });
});
