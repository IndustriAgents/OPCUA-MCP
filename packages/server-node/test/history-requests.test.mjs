import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { rawDetails } from "../build/history.js";

const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/history-requests.json", import.meta.url), "utf8")
).cases;
for (const c of cases) {
  test(`raw history request: ${c.name}`, () => {
    const request = rawDetails(new Date(c.start), new Date(c.end), c.count);
    assert.equal(request.returnBounds, c.returnBounds);
    assert.equal(request.isReadModified, false);
    assert.equal(request.numValuesPerNode, c.count);
    assert.equal(request.startTime.toISOString(), new Date(c.start).toISOString());
    assert.equal(request.endTime.toISOString(), new Date(c.end).toISOString());
  });
}
