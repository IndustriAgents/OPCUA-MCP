import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { readHistory } from "../build/application/history.js";
import { NodeOpcuaHistoryPort } from "../build/adapters/opcua-history.js";
import { AdapterFailure, ContractRefusal, message } from "../build/errors.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/history-port.json", import.meta.url), "utf8")
);
for (const c of fixture.cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [],
      native = new Error("native timeout");
    const record = (name, fields) => {
      calls.push({ name, ...fields });
      if (c.crash === name) throw new AdapterFailure(name, "native timeout", native);
      if (c.refuse === name) throw new ContractRefusal(c.literal);
    };
    const port = {
      async raw(nodeId, start, end, wanted) {
        record("raw", {
          node_id: nodeId,
          start: start?.toISOString() ?? null,
          end: end?.toISOString() ?? null,
          wanted,
        });
        return { records: c.records, continued: c.continued ?? false };
      },
      async aggregate(nodeId, start, end, name, interval) {
        record("aggregate", {
          node_id: nodeId,
          start: start.toISOString(),
          end: end.toISOString(),
          function: name,
          interval,
        });
        return c.records;
      },
    };
    const request = Object.fromEntries(Object.entries(c.request).filter(([, v]) => v !== null));
    const invoke = () => readHistory(port, request, c.offered ?? [], () => new Date(fixture.now));
    if (c.error || c.literal)
      await assert.rejects(invoke, (error) => {
        assert.equal(error.message, c.literal ?? message(c.error, c.fields ?? {}));
        if (c.crash) assert.equal(error.cause.cause, native);
        return true;
      });
    else assert.deepEqual(await invoke(), c.expected);
    assert.deepEqual(calls, c.calls);
    assert.deepEqual(c, before);
  });
it("native raw-history timeout retains its original cause and one attempt", async () => {
  const native = new Error("native timeout");
  native.code = "ETIMEDOUT";
  let calls = 0;
  const port = new NodeOpcuaHistoryPort({
    async readHistoryValue() {
      calls++;
      throw native;
    },
  });
  await assert.rejects(
    () => port.raw("ns=2;i=20", undefined, undefined, 10),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, native);
      return true;
    }
  );
  assert.equal(calls, 1);
});
