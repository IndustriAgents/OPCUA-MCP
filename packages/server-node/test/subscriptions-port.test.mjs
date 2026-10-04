import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import {
  listSubscriptions,
  subscribeNodes,
  unsubscribeNodes,
} from "../build/application/subscriptions.js";
import { NodeOpcuaSubscriptionPort } from "../build/adapters/opcua-subscriptions.js";
import { AdapterFailure, message } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/subscriptions-port.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [],
      native = new Error("native timeout");
    let index = 0;
    const record = (name, fields = {}) => {
      calls.push({ name, ...fields });
      if (c.crash === name) throw new AdapterFailure(name, "native timeout", native);
    };
    const port = {
      list() {
        record("list");
        return c.active;
      },
      async ranges(ids) {
        record("ranges", { node_ids: ids });
        return new Map(Object.entries(c.ranges));
      },
      async subscribe(id, options, filter) {
        record("subscribe", { node_id: id });
        assert.deepEqual(options, c.options);
        assert.deepEqual(filter, c.filter);
        return c.results[index++];
      },
      async unsubscribe(id) {
        record("unsubscribe", { id });
        return c.active.find((r) => r.subscription_id === id);
      },
    };
    const invoke = () =>
      c.operation === "list"
        ? listSubscriptions(port)
        : c.operation === "subscribe"
          ? subscribeNodes(port, c.ids, c.options, c.filter)
          : unsubscribeNodes(port, c.ids);
    if (c.error)
      await assert.rejects(
        async () => invoke(),
        (error) => {
          assert.equal(error.message, message(c.error, c.fields));
          if (c.crash) assert.equal(error.cause.cause, native);
          return true;
        }
      );
    else assert.deepEqual(await invoke(), c.expected);
    assert.deepEqual(calls, c.calls);
    assert.deepEqual(c, before);
  });
it("native subscription timeout keeps original cause and one attempt", async () => {
  const native = new Error("native timeout");
  native.code = "ETIMEDOUT";
  let calls = 0;
  const manager = {
    async subscribe() {
      calls++;
      throw native;
    },
  };
  const port = new NodeOpcuaSubscriptionPort(() => ({}), manager, null);
  await assert.rejects(
    () =>
      port.subscribe(
        "ns=2;i=1",
        {},
        { deadbandType: "none", deadbandValue: 0, trigger: "statusValue" }
      ),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, native);
      return true;
    }
  );
  assert.equal(calls, 1);
});
it("offline list and cancellation never select a session", async () => {
  const manager = {
    list: () => [],
    unsubscribe: async () => ({ subscription_id: "sub-1", dropped: 0 }),
  };
  const port = new NodeOpcuaSubscriptionPort(
    () => {
      throw new Error("session must stay lazy");
    },
    manager,
    null
  );
  assert.deepEqual(port.list(), []);
  assert.equal((await port.unsubscribe("sub-1")).subscription_id, "sub-1");
});
