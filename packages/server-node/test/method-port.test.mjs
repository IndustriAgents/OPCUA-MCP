import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { callMethod } from "../build/application/methods.js";
import { NodeOpcuaMethodPort } from "../build/adapters/opcua-methods.js";
import { AdapterFailure, ContractRefusal } from "../build/errors.js";
import { isConnectionError } from "../build/connection.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/method-port.json", import.meta.url), "utf8")
);
class FakeMethodPort {
  constructor(c) {
    this.c = c;
    this.calls = [];
  }
  async inputTypes(nodeId) {
    this.calls.push(["metadata", nodeId]);
    if (this.c.metadataError) throw new Error(this.c.metadataError);
    return this.c.declared;
  }
  async call(objectId, methodId, args) {
    this.calls.push(["call", objectId, methodId, args]);
    if (this.c.refusal) throw new ContractRefusal(this.c.refusal);
    if (this.c.callError) throw Object.assign(new Error(this.c.callError), { code: "ETIMEDOUT" });
    return { status: this.c.status, outputs: this.c.outputs };
  }
}
for (const c of fixture.cases)
  it(c.name, async () => {
    const before = JSON.stringify(fixture),
      port = new FakeMethodPort(c);
    if (c.error)
      await assert.rejects(
        () => callMethod(port, fixture.objectNodeId, fixture.methodNodeId, c.arguments),
        (e) => e.message === c.error
      );
    else {
      const result = await callMethod(
        port,
        fixture.objectNodeId,
        fixture.methodNodeId,
        c.arguments
      );
      assert.deepEqual(result, {
        object_node_id: "ns=0;i=85",
        method_node_id: fixture.methodNodeId,
        status: c.status,
        outputs: c.outputs,
      });
      assert.deepEqual(port.calls[1][3], c.typed);
    }
    assert.deepEqual(port.calls[0], ["metadata", fixture.methodNodeId]);
    assert.equal(port.calls.filter((c) => c[0] === "call").length, c.metadataError ? 0 : 1);
    assert.equal(JSON.stringify(fixture), before);
  });
it("native timeout retains its cause without another call", async () => {
  const cause = Object.assign(new Error("native timeout"), { code: "ETIMEDOUT" });
  let calls = 0;
  const port = new NodeOpcuaMethodPort({
    call: async () => {
      calls++;
      throw cause;
    },
  });
  await assert.rejects(
    () => port.call("i=85", "ns=2;i=20", []),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, cause);
      assert.equal(isConnectionError(error), true);
      return true;
    }
  );
  assert.equal(calls, 1);
});
