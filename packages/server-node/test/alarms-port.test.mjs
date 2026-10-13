import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { actOnAlarm, listAlarms } from "../build/application/alarms.js";
import { NodeOpcuaAlarmPort } from "../build/adapters/opcua-alarms.js";
import { AdapterFailure, message } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/alarms-port.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [],
      native = new Error("native timeout");
    const record = (name, fields) => {
      calls.push({ name, ...fields });
      if (c.crash === name) throw new AdapterFailure(name, "native timeout", native);
    };
    const port = {
      async list(nodeId, timeout) {
        record("list", { node_id: nodeId, timeout });
        return c.records;
      },
      remember(records) {
        record("remember", { records });
      },
      conditionFor(eventId) {
        record("condition", { event_id: eventId });
        return c.cached;
      },
      async action(conditionId, eventId, action, comment, duration) {
        record("action", {
          condition_id: conditionId,
          event_id: eventId,
          action,
          comment,
          duration,
        });
        return c.status;
      },
    };
    const invoke = () =>
      c.operation === "list"
        ? listAlarms(port, c.node_id, c.timeout)
        : actOnAlarm(
            port,
            c.event_id,
            c.action,
            c.comment,
            c.duration,
            c.condition_id,
            c.acknowledgement
          );
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
it("native alarm timeout keeps its original cause and one call", async () => {
  const native = new Error("native timeout");
  native.code = "ETIMEDOUT";
  let calls = 0;
  const session = {
    async browse() {
      return { references: [] };
    },
    async call() {
      calls++;
      throw native;
    },
  };
  const port = new NodeOpcuaAlarmPort(() => session, null);
  await assert.rejects(
    () => port.action("ns=2;i=7", "AQ==", "acknowledge", "", null),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, native);
      return true;
    }
  );
  assert.equal(calls, 1);
});
