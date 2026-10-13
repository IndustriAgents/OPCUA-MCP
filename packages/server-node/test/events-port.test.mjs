import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { subscribeEvents, readEvents, readEventHistory } from "../build/application/events.js";
import { NodeOpcuaEventPort } from "../build/adapters/opcua-events.js";
import { AdapterFailure, ContractRefusal, message } from "../build/errors.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/events-port.json", import.meta.url), "utf8")
);
for (const c of fixture.cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [],
      native = new Error("native timeout");
    const record = (name, fields) => {
      calls.push({ name, ...fields });
      if (c.crash === name) throw new AdapterFailure(name, "native timeout", native);
    };
    const port = {
      async subscribe(nodeId, severity, size) {
        record("subscribe", { node_id: nodeId, severity, size });
        return c.replaced;
      },
      async drain(nodeId, limit) {
        record("drain", { node_id: nodeId, limit });
        return c.drain;
      },
      async history(nodeId, start, end, wanted, severity) {
        record("history", {
          node_id: nodeId,
          start: start.toISOString(),
          end: end.toISOString(),
          wanted,
          severity,
        });
        return c.page;
      },
    };
    const invoke = () =>
      c.operation === "subscribe"
        ? subscribeEvents(port, c.nodeId, c.severity, c.requested)
        : c.operation === "drain"
          ? readEvents(port, c.nodeId, c.limit)
          : readEventHistory(port, c.request, () => new Date(fixture.now));
    if (c.error)
      await assert.rejects(invoke, (error) => {
        assert.equal(error.message, message(c.error, c.fields));
        if (c.crash) assert.equal(error.cause.cause, native);
        return true;
      });
    else assert.deepEqual(await invoke(), c.expected);
    assert.deepEqual(calls, c.calls);
    assert.deepEqual(c, before);
  });
it("native event subscription timeout retains its original cause and one attempt", async () => {
  const native = new Error("native timeout");
  native.code = "ETIMEDOUT";
  let calls = 0;
  const port = new NodeOpcuaEventPort(null, {
    async subscribe() {
      calls++;
      throw native;
    },
  });
  await assert.rejects(
    () => port.subscribe("i=85", 0, 10),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, native);
      return true;
    }
  );
  assert.equal(calls, 1);
});
