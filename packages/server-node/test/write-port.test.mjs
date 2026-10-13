import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { writeNodes } from "../build/application/write.js";
import { NodeOpcuaWritePort } from "../build/adapters/opcua-write.js";
import { AdapterFailure, ContractRefusal, message } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/write-port.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const before = structuredClone(c);
    const calls = [];
    const native = new Error("native timeout");
    let prepared = 0;
    const record = (name, fields = {}) => {
      calls.push({ name, ...fields });
      if (c.crash === name) throw new AdapterFailure(name, "native timeout", native);
    };
    const port = {
      async current(nodes, indices, chunk) {
        record("current", { indices, chunk });
        return new Map(Object.entries(c.current ?? {}).map(([k, v]) => [Number(k), v]));
      },
      async engineering(nodeIds) {
        record("engineering", { node_ids: nodeIds });
        return new Map(Object.entries(c.engineering ?? {}));
      },
      async prepare(node, index) {
        record("prepare", { index });
        if (c.refuse_at === index) throw new ContractRefusal(c.refusal);
        const failure = c.failures?.[index];
        if (!failure) prepared++;
        return failure ?? null;
      },
      async send() {
        record("send");
        return c.statuses ?? Array(prepared).fill("Good");
      },
    };
    const invoke = () =>
      writeNodes(
        port,
        c.nodes,
        c.limits,
        new Map(Object.entries(c.bounds).map(([k, v]) => [Number(k), v])),
        c.allow_out_of_range
      );
    if (c.error || c.refusal)
      await assert.rejects(invoke, (error) => {
        assert.equal(error.message, c.refusal ?? message(c.error, c.fields ?? {}));
        if (c.crash) assert.equal(error.cause.cause, native);
        return true;
      });
    else assert.deepEqual(await invoke(), c.expected);
    assert.deepEqual(calls, c.calls);
    assert.deepEqual(c, before);
  });
it("native write timeout keeps the original cause and one attempt", async () => {
  const native = new Error("native timeout");
  native.code = "ETIMEDOUT";
  let attempts = 0;
  const port = new NodeOpcuaWritePort(
    {
      async write() {
        attempts++;
        throw native;
      },
    },
    null
  );
  await assert.rejects(
    () => port.send(),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, native);
      return true;
    }
  );
  assert.equal(attempts, 1);
});
