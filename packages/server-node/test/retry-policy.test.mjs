// What may follow a request that died on its session (issues #105, #106, #107,
// #108).
//
// The question this settles is not "is this tool idempotent" — that is
// `annotations.idempotentHint`, which is advice to the *model*. It is "may this
// server put a second request on the wire when it does not know what happened to
// the first", which is `contract/tools.json` -> `retryPolicy`. Both runtimes used
// to read the annotation for both, which is how `write_opcua_nodes` came to be
// automatically re-sent after a lost response.
//
// `tests/unit/test_retry_policy.py` is the Python half and drives the same
// shared table. Needs no OPC UA server: the connection and the dispatch are both
// stubbed, so what is under test is the dispatcher's decisions rather than
// node-opcua's behaviour.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { CONTRACT } from "../build/contract.js";
import { OpcuaConnection } from "../build/connection.js";
import { message } from "../build/errors.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { OpcuaTools, describeTargets } from "../build/tools.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "uncertain-outcome.json"), "utf8")
);
const byName = new Map(CONTRACT.tools.map((tool) => [tool.name, tool]));
const POLICIES = new Set(Object.keys(CONTRACT.retryPolicies).filter((key) => !key.startsWith("$")));

/** A dead-session failure, worded the way the client library words one. */
function deadSession() {
  return new Error("BadSessionIdInvalid");
}

/** An `OpcuaTools` whose connection and dispatch are both under the test's control.
 *
 * `dispatch` is replaced rather than driven, so a test can say what the OPC UA
 * layer did without a server: each entry in `outcomes` is thrown or returned for
 * one attempt, and `attempts` records what was actually put on the wire.
 */
function harness({ env = {}, outcomes = [], connected = true } = {}) {
  const conn = new OpcuaConnection();
  const state = { reconnects: 0, attempts: [], connects: 0, namespaces: null };

  conn.ensureConnection = async () => {
    state.connects += 1;
    if (!connected) throw new Error("ECONNREFUSED");
  };
  conn.reconnect = async () => {
    state.reconnects += 1;
    // What a real reconnect does and what makes re-authorization necessary: the
    // restarted server may have loaded its namespaces in a different order.
    if (state.namespaces) policy.bindNamespaces(state.namespaces);
  };
  conn.accessHistoryDataCapability = async () => false;
  conn.serverCapabilitiesAggregateFunctions = async () => [];

  const policy = new ToolPolicy(parsePolicyConfig(env));
  const tools = new OpcuaTools(conn, policy);
  tools.dispatch = async (name, args) => {
    state.attempts.push({ name, args });
    const outcome = outcomes[state.attempts.length - 1];
    if (outcome instanceof Error) throw outcome;
    return outcome ?? { content: [], structuredContent: { result: [] } };
  };
  return { tools, policy, state };
}

/** Everything the audit trail wrote during `run`. */
async function auditing(run) {
  const lines = [];
  const original = console.error;
  console.error = (...parts) => {
    const [first] = parts;
    if (typeof first === "string" && first.startsWith('{"event":"opcua_mcp_policy"')) {
      lines.push(JSON.parse(first));
    }
  };
  try {
    return { result: await run(), lines };
  } finally {
    console.error = original;
  }
}

const OPERATOR = {
  OPCUA_PROFILE: "operator",
  OPCUA_SECURITY_POLICY: "Basic256Sha256",
  OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=5",
};

describe("the contract's retry policy", () => {
  it("is declared, and known, on every tool", () => {
    for (const tool of CONTRACT.tools) {
      assert.ok(
        POLICIES.has(tool.retryPolicy),
        `${tool.name} declares retryPolicy ${tool.retryPolicy}`
      );
    }
  });

  it("never re-sends a control request", () => {
    for (const tool of CONTRACT.tools) {
      if (tool.accessClass === "control" || tool.accessClass === "alarm-action") {
        assert.equal(tool.retryPolicy, "uncertainOutcome", tool.name);
      }
    }
  });

  // The regression the field exists for. `write_opcua_nodes` carries
  // `idempotentHint: true` and always should — writing 99.9 twice leaves 99.9,
  // which is what that annotation tells the model. It is the transport that must
  // not read it.
  it("is not the idempotent hint", () => {
    const write = byName.get("write_opcua_nodes");
    assert.equal(write.annotations.idempotentHint, true);
    assert.equal(write.retryPolicy, "uncertainOutcome");
  });

  it("is not advertised to clients", () => {
    for (const tool of CONTRACT.tools) {
      assert.equal("retryPolicy" in tool.annotations, false, tool.name);
    }
  });
});

describe("describeTargets", () => {
  for (const testCase of FIXTURE.cases) {
    // The Python half asserts the same sentence for the same call.
    it(testCase.name, () => {
      assert.equal(
        describeTargets(byName.get(testCase.tool), testCase.arguments),
        testCase.targets
      );
    });
  }

  it("never carries the value being written", () => {
    const targets = describeTargets(byName.get("write_opcua_nodes"), {
      nodes: [{ node_id: "ns=2;i=5", value: 1234.5 }],
    });
    assert.equal(targets.includes("1234.5"), false);
  });

  it("says so when the tool declares no guard", () => {
    assert.equal(
      describeTargets(byName.get("read_opcua_nodes"), { node_ids: ["ns=2;i=1"] }),
      "unknown"
    );
  });
});

describe("a call that dies on its session", () => {
  it("re-sends a read on the fresh session", async () => {
    const { tools, state } = harness({
      outcomes: [deadSession(), { content: [], structuredContent: { result: ["ok"] } }],
    });

    const result = await tools.callTool({
      params: { name: "read_opcua_nodes", arguments: { node_ids: ["ns=2;i=1"] } },
    });

    assert.equal(state.reconnects, 1);
    assert.equal(state.attempts.length, 2, "a read must be retried");
    assert.deepEqual(result.structuredContent.result, ["ok"]);
  });

  // The core of #106. A lost response does not prove the write failed.
  it("does not re-send a write, and says the outcome is unknown", async () => {
    const { tools, state } = harness({
      env: OPERATOR,
      outcomes: [deadSession()],
    });

    const result = await tools.callTool({
      params: {
        name: "write_opcua_nodes",
        arguments: { nodes: [{ node_id: "ns=2;i=5", value: 99.9 }] },
      },
    });

    assert.equal(state.attempts.length, 1, "a write must never reach the plant twice");
    assert.equal(state.reconnects, 1, "the connection is still rebuilt for the next call");
    assert.equal(result.isError, true);
    assert.equal(
      result.content[0].text,
      message("uncertainOutcome", {
        tool: "write_opcua_nodes",
        reason: "BadSessionIdInvalid",
        targets: "node_ids=ns=2;i=5",
      })
    );
  });

  it("rebuilds the connection for a monitor tool but reports the original failure", async () => {
    const { tools, state } = harness({ outcomes: [deadSession()] });

    const result = await tools.callTool({
      params: { name: "subscribe_opcua_nodes", arguments: { node_ids: ["ns=2;i=1"] } },
    });

    assert.equal(state.attempts.length, 1);
    assert.equal(state.reconnects, 1);
    assert.equal(result.isError, true);
    // The subscription lived on the session, so it died with it: the failure is
    // complete rather than uncertain, and is reported as it came.
    assert.equal(result.content[0].text, "BadSessionIdInvalid");
  });
});

describe("authorization across the retry", () => {
  // The core of #105. A namespace index is assigned per session, so a server
  // that restarted with its namespaces in a different order moves the node an
  // `ns=` allowlist entry points at — and the first attempt was authorized
  // against the old order. Nothing the contract declares today is both
  // allowlisted and re-sent (every control tool is `uncertainOutcome` after
  // #106), so what is pinned here is the invariant itself: the second attempt is
  // authorized, and a refusal on it stops the request.
  it("authorizes every physical attempt, not every call", async () => {
    const { tools, policy, state } = harness({
      outcomes: [deadSession(), { content: [], structuredContent: { result: ["ok"] } }],
    });
    const authorized = [];
    const original = policy.authorize.bind(policy);
    policy.authorize = (name, args) => {
      authorized.push(name);
      return original(name, args);
    };

    await tools.callTool({
      params: { name: "read_opcua_nodes", arguments: { node_ids: ["ns=2;i=5"] } },
    });

    assert.equal(state.attempts.length, 2);
    assert.deepEqual(authorized, ["read_opcua_nodes", "read_opcua_nodes"]);
  });

  it("stops the second attempt when the fresh session no longer authorizes it", async () => {
    const { tools, policy, state } = harness({
      outcomes: [deadSession(), { content: [], structuredContent: { result: ["ok"] } }],
    });
    // What a namespace renumbering looks like from the policy's side: the same
    // node id, resolved against a different NamespaceArray, is no longer on the
    // allowlist.
    let calls = 0;
    policy.authorize = () => {
      calls += 1;
      if (calls > 1) throw new Error("Node ns=2;i=5 is not writable under the operator policy");
    };

    const result = await tools.callTool({
      params: { name: "read_opcua_nodes", arguments: { node_ids: ["ns=2;i=5"] } },
    });

    assert.equal(state.attempts.length, 1, "a request nobody authorized must not be sent");
    assert.equal(result.isError, true);
    assert.equal(result.content[0].text, "Node ns=2;i=5 is not writable under the operator policy");
  });

  it("re-binds the namespaces before it re-authorizes", async () => {
    // Ordering, because the check is only worth anything against the mapping the
    // second attempt will actually resolve against. `reconnect()` re-reads the
    // server's NamespaceArray; `authorize()` must run after it, not before.
    const { tools, policy, state } = harness({
      outcomes: [deadSession(), { content: [], structuredContent: { result: [] } }],
    });
    const order = [];
    state.namespaces = ["http://opcfoundation.org/UA/", "http://example.org/plant"];
    const bind = policy.bindNamespaces.bind(policy);
    policy.bindNamespaces = (uris) => {
      order.push("bind");
      return bind(uris);
    };
    policy.authorize = () => {
      order.push("authorize");
    };

    await tools.callTool({
      params: { name: "read_opcua_nodes", arguments: { node_ids: ["ns=1;i=5"] } },
    });

    assert.deepEqual(order, ["authorize", "bind", "authorize"]);
  });

  it("audits the second attempt as its own, with its own attempt number", async () => {
    const { tools, state } = harness({
      env: OPERATOR,
      outcomes: [deadSession()],
    });

    const { lines } = await auditing(() =>
      tools.callTool({
        params: {
          name: "write_opcua_nodes",
          arguments: { nodes: [{ node_id: "ns=2;i=5", value: 99.9 }] },
        },
      })
    );

    assert.equal(state.attempts.length, 1);
    assert.deepEqual(
      lines.map((line) => [line.decision, line.attempt]),
      [
        ["allowed", 1],
        ["failed", 1],
      ]
    );
    // One call, one story: both lines carry the same id.
    assert.equal(new Set(lines.map((line) => line.call_id)).size, 1);
  });

  it("writes an allowed line per physical attempt when a read is re-sent", async () => {
    // Reads are not audited — that would bury the lines anyone is looking for —
    // so the counterpart is checked on the one path that does audit and does
    // retry nothing: the numbers must still be attempt-scoped rather than
    // per-call.
    const { tools } = harness({
      env: OPERATOR,
      outcomes: [{ content: [], structuredContent: { result: [] } }],
    });
    const { lines } = await auditing(() =>
      tools.callTool({
        params: {
          name: "write_opcua_nodes",
          arguments: { nodes: [{ node_id: "ns=2;i=5", value: 1 }] },
        },
      })
    );
    assert.deepEqual(
      lines.map((line) => [line.decision, line.attempt]),
      [
        ["allowed", 1],
        ["completed", 1],
      ]
    );
  });
});

describe("the capability gate", () => {
  // #108: the capability map is filled in by the reconnect callback, so on a
  // process that started while the plant was unreachable it still holds its
  // startup defaults. Checking it before connecting refused the tool without
  // ever asking the server.
  it("is checked after the connection, not before it", async () => {
    const { tools, state } = harness({ connected: false });

    const result = await tools.callTool({
      params: { name: "read_opcua_history", arguments: { node_id: "ns=2;i=1" } },
    });

    assert.equal(state.connects, 1, "the server must be asked before it is judged");
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /Not connected to the OPC UA server/);
    assert.equal(
      result.content[0].text.includes("advertises none of"),
      false,
      "a plant that could not be reached is not a plant that lacks the capability"
    );
  });
});

describe("OpcuaConnection.reconnect", () => {
  // #107. `connect()` was already single-flight, so two rebuilds could not both
  // open a client — but they could interleave so that one tore down the fresh
  // session the other had just opened and was about to return.
  // The race itself, driven through the real `teardown` and the real state
  // machine: only `open()` is stubbed, because that is the only part that needs
  // a network. Before the fix this ended with `connected === false` — B's
  // teardown awaited A's `connectPromise`, then closed the session A had just
  // opened and was about to hand back.
  it("leaves one live session when two callers rebuild at once", async () => {
    const connection = new OpcuaConnection();
    let opened = 0;
    const closed = [];
    connection.open = async () => {
      const id = ++opened;
      // A real open is not instantaneous, which is what lets the second caller
      // reach `teardown` while the first is still in `connect`.
      await new Promise((resolve) => setImmediate(resolve));
      connection.session = { id, close: async () => closed.push(id) };
      connection.opcuaClient = { disconnect: async () => {} };
      connection.state = "connected";
    };

    await Promise.all([connection.reconnect(), connection.reconnect()]);

    assert.equal(connection.connected, true, "the surviving session must be live");
    assert.equal(
      closed.includes(connection.session.id),
      false,
      "nobody may close the session the callers were handed"
    );
  });

  it("is single-flight, as one teardown-and-open transaction", async () => {
    const connection = new OpcuaConnection();
    let rebuilds = 0;
    let release;
    connection.rebuild = async () => {
      rebuilds += 1;
      await new Promise((resolve) => {
        release = resolve;
      });
    };

    const first = connection.reconnect();
    const second = connection.reconnect();
    assert.equal(rebuilds, 1);
    release();
    await Promise.all([first, second]);
    assert.equal(rebuilds, 1);
  });

  it("lets the next caller start a new rebuild once the first has finished", async () => {
    const connection = new OpcuaConnection();
    let rebuilds = 0;
    connection.rebuild = async () => {
      rebuilds += 1;
    };

    await connection.reconnect();
    await connection.reconnect();
    assert.equal(rebuilds, 2);
  });

  it("releases the claim when a rebuild fails, so a later call can try again", async () => {
    const connection = new OpcuaConnection();
    let rebuilds = 0;
    connection.rebuild = async () => {
      rebuilds += 1;
      throw new Error("ECONNREFUSED");
    };

    await assert.rejects(() => connection.reconnect());
    await assert.rejects(() => connection.reconnect());
    assert.equal(rebuilds, 2);
  });
});
