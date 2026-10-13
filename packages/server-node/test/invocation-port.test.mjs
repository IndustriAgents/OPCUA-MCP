import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { invokeTool } from "../build/application/invocation.js";
import { CONTRACT } from "../build/contract.js";
import { ContractRefusal, message } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/invocation-port.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const before = structuredClone(c),
      calls = [],
      native = new Error("native timeout");
    let caps = 0,
      dispatches = 0;
    const record = (name) => {
      calls.push(name);
      if (c.crash === name)
        throw ["connect", "reconnect:old-session"].includes(name)
          ? native
          : new ContractRefusal("test refusal");
    };
    const call = {
      name: c.tool,
      arguments: {},
      spec: CONTRACT.tools.find((t) => t.name === c.tool),
      callId: "id",
      attempt: 1,
      denied: false,
      session: null,
    };
    const port = {
      endpoint: () => "opc.tcp://test:4840",
      session() {
        record("session");
        return "old-session";
      },
      hasConnection: () => true,
      async waitForWarmUp() {
        record("warm-up");
      },
      async connect() {
        record("connect");
      },
      async capabilities() {
        record(`capabilities:${++caps}`);
      },
      async dispatch() {
        record(`dispatch:${++dispatches}`);
        if (c.tool !== "get_server_status" && dispatches === 1) throw native;
        if (c.secondFailure) throw new Error("second failure");
        return { ok: true };
      },
      isConnectionError: (error) => error === native && c.dead !== false,
      async reconnect(session) {
        record(`reconnect:${session}`);
      },
      logRecovery(resend) {
        record(`log:${resend ? "resend" : "once"}`);
      },
      targets: () => "ns=2;i=5",
      authorize(call) {
        record(`authorize:${call.attempt}`);
      },
      allowed(call) {
        record(`allowed:${call.attempt}`);
      },
      denied(call) {
        record(`denied:${call.attempt}`);
      },
    };
    if (c.error || c.raw)
      await assert.rejects(
        () => invokeTool(port, call),
        (error) => {
          assert.equal(error.message, c.error ? message(c.error, c.fields) : c.raw);
          if (c.error) assert.equal(error.cause, native);
          return true;
        }
      );
    else assert.deepEqual(await invokeTool(port, call), c.result);
    assert.deepEqual(calls, c.calls);
    assert.equal(call.attempt, c.attempt);
    assert.equal(call.denied, c.denied ?? false);
    assert.deepEqual(c, before);
  });
