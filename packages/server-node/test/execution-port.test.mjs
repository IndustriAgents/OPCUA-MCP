import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { executeTool } from "../build/application/execution.js";
import { ContractRefusal } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/execution-port.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [],
      original = new Error("private native details");
    const record = (name) => {
      calls.push(name);
      if (c.crash === name) throw name === "run" ? original : new ContractRefusal("test refusal");
    };
    const port = {
      newCallId() {
        record("id");
        return "0123456789abcdef";
      },
      async waitForConnection() {
        record("wait");
      },
      authorize(name, args) {
        assert.equal(name, c.tool);
        assert.deepEqual(args, c.arguments);
        record("authorize");
      },
      allowed(call) {
        assert.equal(call.callId, "0123456789abcdef");
        record(`allowed:${call.attempt}`);
        if (c.crash === "allowed") throw new ContractRefusal("test refusal");
      },
      after(call, decision, reason) {
        record(`${decision}:${call.attempt}`);
        if (decision === "failed") assert.equal(reason, "safe operation failure");
      },
      async run(call) {
        if (c.attempt) call.attempt = c.attempt;
        if (c.denied) {
          call.denied = true;
          port.after(call, "denied", "policy changed");
        }
        record("run");
        return c.result;
      },
      normalizeFailure(_name, error) {
        record("normalize-failure");
        assert.equal(error, original);
        return new ContractRefusal("safe operation failure", { cause: error });
      },
      normalizeResult(result) {
        record("normalize-result");
        return result;
      },
    };
    // The recovery denial happens after the operation is entered, like reconnect.
    const run = port.run;
    port.run = async (call) => {
      if (c.denied) {
        calls.push("run");
        call.attempt = c.attempt;
        call.denied = true;
        port.after(call, "denied", "policy changed");
        throw original;
      }
      return await run(call);
    };
    if (c.error)
      await assert.rejects(
        () => executeTool(port, c.tool, c.arguments),
        (error) => {
          assert.equal(error.message, c.error);
          return true;
        }
      );
    else assert.deepEqual(await executeTool(port, c.tool, c.arguments), c.result);
    assert.deepEqual(calls, c.calls);
    assert.deepEqual(c, before);
  });
it("invalid shape is denied before waiting, authorization or operation", async () => {
  const calls = [];
  const port = {
    newCallId: () => "id",
    after(_call, decision) {
      calls.push(decision);
    },
    waitForConnection() {
      throw new Error("must not wait");
    },
    authorize() {
      throw new Error("must not authorize");
    },
    allowed() {
      throw new Error("must not allow");
    },
    run() {
      throw new Error("must not run");
    },
  };
  await assert.rejects(() => executeTool(port, "write_opcua_nodes", { nodes: [] }), /nodes/);
  assert.deepEqual(calls, ["denied"]);
});
