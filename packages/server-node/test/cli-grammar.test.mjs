import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { parseArgs } from "../build/install.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/cli-cases.json", import.meta.url), "utf8")
);
for (const c of fixture.cases)
  it(c.name, () => {
    const action = parseArgs(c.argv, fixture.defaultUrl);
    assert.equal(action.kind, c.kind);
    if ("message" in c) assert.equal(action.message, c.message);
    if ("settings" in c) assert.deepEqual(action.options.settings, c.settings);
  });
