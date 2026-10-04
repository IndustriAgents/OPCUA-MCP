import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { getServerStatus } from "../build/application/diagnostics.js";
import { NodeOpcuaDiagnosticsPort } from "../build/adapters/opcua-diagnostics.js";
import { AdapterFailure } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/diagnostics-port.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [];
    let generation = 1;
    const port = {
      snapshot() {
        calls.push("snapshot");
        return c.snapshot;
      },
      async read(security, identity) {
        calls.push("read");
        assert.equal(security, c.security);
        assert.deepEqual(identity, c.identity);
        if (c.crash)
          throw new AdapterFailure("diagnostics", "native timeout", new Error("native timeout"));
        generation = 2;
        return c.status;
      },
      capabilities() {
        calls.push("capabilities");
        return { session_generation: generation };
      },
    };
    assert.deepEqual(await getServerStatus(port, c.security, c.identity), c.expected);
    assert.deepEqual(calls, c.calls);
    assert.deepEqual(c, before);
  });
it("native diagnostics timeout retains cause before retry classification", async () => {
  const native = new Error("native timeout");
  native.code = "ETIMEDOUT";
  let calls = 0;
  let classified;
  const conn = {
    endpointUrl: "opc.tcp://test:4840",
    async withRetry(read) {
      try {
        return await read();
      } catch (error) {
        classified = error;
        throw error;
      }
    },
  };
  const port = new NodeOpcuaDiagnosticsPort(
    conn,
    () => ({
      async readVariableValue() {
        calls++;
        throw native;
      },
    }),
    () => ({})
  );
  await assert.rejects(
    () => port.read("None", cases[0].identity),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, native);
      assert.equal(error, classified);
      return true;
    }
  );
  assert.equal(calls, 1);
});
