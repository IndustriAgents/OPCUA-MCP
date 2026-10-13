import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { prettyJson } from "../build/result-text.js";
import { toHistoryRecord } from "../build/records.js";
import { DataValue, Variant, DataType } from "node-opcua-client";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/result-text.json", import.meta.url), "utf8")
);
describe("canonical result text", () => {
  for (const c of fixture.cases)
    it(c.name, () => {
      assert.equal(prettyJson(c.value), c.pretty);
      assert.equal(JSON.stringify(c.value), c.compact);
    });
  for (const c of fixture.timestamps)
    it(c.name, () => {
      const value = new DataValue({
        value: new Variant({ dataType: DataType.Double, value: 1 }),
        sourceTimestamp: new Date(c.input),
      });
      assert.equal(toHistoryRecord(value).timestamp, c.node);
    });
});
