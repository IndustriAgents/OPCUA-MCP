import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { readNodes } from "../build/application/read.js";
import { NodeOpcuaReadPort } from "../build/adapters/opcua-read.js";
import { AdapterFailure, ContractRefusal } from "../build/errors.js";
import { isConnectionError } from "../build/connection.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/read-port.json", import.meta.url), "utf8")
);
function fake() {
  const calls = [];
  return {
    calls,
    async values(ids) {
      calls.push(["values", ids]);
      return fixture.records.filter((record) => ids.includes(record.node_id));
    },
    async engineering(ids) {
      calls.push(["engineering", ids]);
      return new Map(Object.entries(fixture.engineering));
    },
  };
}
it("fake port preserves batch order, statuses and metadata", async () => {
  const port = fake();
  const before = JSON.stringify(fixture);
  const actual = await readNodes(port, fixture.nodeIds, fixture.chunk);
  assert.deepEqual(
    actual,
    fixture.records.map((record) => ({
      ...record,
      engineering: fixture.engineering[record.node_id] ?? null,
    }))
  );
  assert.deepEqual(port.calls, fixture.calls);
  assert.equal(JSON.stringify(fixture), before);
});
it("empty reads never touch the port", async () => {
  const port = fake();
  await assert.rejects(() => readNodes(port, [], fixture.chunk), ContractRefusal);
  assert.deepEqual(port.calls, []);
});
it("adapter translates socket failure and retains retry cause", async () => {
  const error = Object.assign(new Error("native timeout"), { code: "ETIMEDOUT" });
  const port = new NodeOpcuaReadPort(
    {
      async read() {
        throw error;
      },
    },
    null
  );
  await assert.rejects(
    () => port.values(fixture.nodeIds),
    (failure) => {
      assert.ok(failure instanceof AdapterFailure);
      assert.equal(failure.operation, "read");
      assert.equal(failure.cause, error);
      assert.ok(isConnectionError(failure));
      assert.equal(failure.message, "Failed to read nodes: native timeout");
      return true;
    }
  );
});
it("absent native metadata is not a failed read", async () => {
  const port = new NodeOpcuaReadPort(null, {
    async forNodes() {
      return new Map([[fixture.nodeIds[0], null]]);
    },
  });
  assert.deepEqual(await port.engineering(fixture.nodeIds), new Map([[fixture.nodeIds[0], null]]));
});
