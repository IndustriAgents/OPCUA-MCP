// Connection resilience: the retry settings, and what counts as a dead session.
//
// The Python server's `tests/unit/test_reconnect.py` asserts the same behaviour
// and additionally compares the two implementations' answers directly. This file
// is the Node-side detail: the parsing rules, and the reasoning about which
// errors are worth reconnecting for.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  RECONNECT_DEFAULTS,
  connectionStrategy,
  describeReconnect,
  parseReconnectConfig,
  reconnectBudgetMs,
  reconnectDelays,
} from "../build/config.js";
import {
  OpcuaConnection,
  isConnectionError,
  notConnectedMessage,
  stillConnectingMessage,
} from "../build/connection.js";
import { CONTRACT } from "../build/contract.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { OpcuaTools } from "../build/tools.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
// The shared table; `tests/unit/test_reconnect.py` drives it too.
const SETTINGS = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "reconnect-settings.json"), "utf8")
);

/** An endpoint that refuses at once: nothing listens on port 1. */
const REFUSING = "opc.tcp://127.0.0.1:1/unreachable";

describe("reconnect settings", () => {
  it("defaults to retrying, without hanging a tool call", () => {
    assert.deepEqual(parseReconnectConfig({}), RECONNECT_DEFAULTS);
    assert.ok(RECONNECT_DEFAULTS.maxRetry > 0, "a dropped connection must be retried by default");
    assert.ok(reconnectBudgetMs(RECONNECT_DEFAULTS) <= 15000);
  });

  it("treats an empty value as unset", () => {
    // An MCP client config that writes `"OPCUA_RECONNECT_MAX_RETRY": ""` means
    // "leave it alone", not "zero".
    assert.deepEqual(parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: "  " }), RECONNECT_DEFAULTS);
  });

  it("refuses a setting it cannot honour", () => {
    for (const [name, value] of [
      ["OPCUA_RECONNECT_INITIAL_DELAY_MS", "soon"],
      ["OPCUA_RECONNECT_INITIAL_DELAY_MS", "-1"],
      ["OPCUA_RECONNECT_MAX_DELAY_MS", "-5"],
      ["OPCUA_RECONNECT_MAX_RETRY", "many"],
      ["OPCUA_RECONNECT_MAX_RETRY", "-2"],
      ["OPCUA_SESSION_TIMEOUT_MS", "10"],
    ]) {
      assert.throws(
        () => parseReconnectConfig({ [name]: value }),
        new RegExp(name),
        `${name}=${value}`
      );
    }
  });

  it("spells unlimited retries as -1", () => {
    assert.equal(parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: "-1" }).maxRetry, -1);
    assert.match(
      describeReconnect(parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: "-1" })),
      /unlimited/
    );
  });

  it("waits the doubling sequence it configured, and no longer", () => {
    // 250 + 500 + 1000 + 1000 + 1000, the ceiling holding after the third.
    const config = { initialDelay: 250, maxDelay: 1000, maxRetry: 5, sessionTimeout: 60000 };
    assert.equal(reconnectBudgetMs(config), 3750);
  });

  it("gives an unlimited retry a finite budget", () => {
    // A tool call waits out this budget before rebuilding the client itself, so
    // "forever" here would mean a request that never returns.
    const budget = reconnectBudgetMs({ ...RECONNECT_DEFAULTS, maxRetry: -1 });
    assert.ok(Number.isFinite(budget) && budget > 0);
  });

  it("still waits once when retrying is switched off", () => {
    // maxRetry 0 hands node-opcua a single attempt; the budget is the floor
    // rather than zero so a repair in flight is not abandoned instantly.
    assert.equal(
      reconnectBudgetMs({ ...RECONNECT_DEFAULTS, maxRetry: 0 }),
      RECONNECT_DEFAULTS.initialDelay
    );
  });

  for (const testCase of SETTINGS.cases) {
    it(`reads OPCUA_RECONNECT_MAX_RETRY=${JSON.stringify(testCase.value)} as the shared table says`, () => {
      const env = { OPCUA_RECONNECT_MAX_RETRY: testCase.value };
      if (testCase.error) {
        assert.throws(() => parseReconnectConfig(env), { message: testCase.error });
        return;
      }
      const config = parseReconnectConfig(env);
      assert.ok(Object.is(config.maxRetry, testCase.maxRetry), `got ${config.maxRetry}`);
      if (testCase.delays) {
        assert.deepEqual(
          reconnectDelays({
            ...config,
            initialDelay: SETTINGS.initialDelay,
            maxDelay: SETTINGS.maxDelay,
          }),
          testCase.delays
        );
      }
    });
  }

  it("describes the settings for the startup log", () => {
    assert.equal(
      describeReconnect({ initialDelay: 250, maxDelay: 1000, maxRetry: 8, sessionTimeout: 30000 }),
      "retries=8 backoff=250..1000ms session-timeout=30000ms"
    );
  });
});

describe("dead-session detection", () => {
  it("looks inside a rewrapped message", () => {
    // Tool bodies re-throw as prose, so the status code arrives embedded.
    assert.ok(isConnectionError(new Error("Failed to read node ns=2;i=3: BadSessionIdInvalid")));
    assert.ok(isConnectionError(new Error("connect ECONNREFUSED 127.0.0.1:4840")));
  });

  it("does not mistake a failed request for a failed connection", () => {
    // Retrying any of these on a fresh session would fail the same way.
    for (const message of [
      "Failed to read node ns=2;i=999999: BadNodeIdUnknown",
      'Invalid date/time: "nope". Use ISO 8601, e.g. 2026-04-23T17:40:00Z',
      "BadOutOfRange",
      "",
    ]) {
      assert.equal(isConnectionError(new Error(message)), false, message);
    }
  });

  it("words the not-connected error the way both runtimes word it", () => {
    assert.equal(
      notConnectedMessage("opc.tcp://plc:4840", "ECONNREFUSED"),
      // The code first, so a client can tell an outage from a capability the
      // server lacks without parsing the prose (#140).
      "endpoint_offline: Not connected to the OPC UA server at opc.tcp://plc:4840: ECONNREFUSED. " +
        "Call get_server_status for details."
    );
  });
});

describe("the diagnostics tool", () => {
  it("is a read tool with no capability gate", () => {
    // ServerStatus is mandatory in OPC UA, and a status report that an
    // observe-only deployment could not call would be useless precisely when it
    // is most needed.
    const tool = CONTRACT.tools.find((candidate) => candidate.name === "get_server_status");
    assert.ok(tool, "get_server_status is missing from the contract");
    assert.equal(tool.accessClass, "read");
    assert.deepEqual(tool.capabilities, []);
    assert.equal(tool.annotations.readOnlyHint, true);
    assert.equal(tool.resultShape, "serverStatus");
  });
});

// #136. These drive the real node-opcua client against a port nothing listens
// on, because what was broken was node-opcua's reading of `maxRetry: -1` — a
// stub would have agreed with whatever this module told it. Every test carries a
// timeout, so a regression fails here rather than hanging the suite.
describe("one connection round", () => {
  const quietly = async (run) => {
    const original = console.error;
    console.error = () => {};
    try {
      return await run();
    } finally {
      console.error = original;
    }
  };

  it("ends, even with unlimited retries", { timeout: 15000 }, async () => {
    // Five attempts 50..100ms apart. Before the fix node-opcua was handed -1 and
    // this connect never settled.
    const connection = new OpcuaConnection(REFUSING, undefined, {
      initialDelay: 50,
      maxDelay: 100,
      maxRetry: -1,
      sessionTimeout: 60000,
    });
    await quietly(() => assert.rejects(connection.connect()));
    assert.equal(connection.connected, false);
    assert.equal(connection.connecting, false);
    assert.ok(connection.lastErrorMessage, "a failed round must say why");
  });

  it("accepts a backoff that does not grow, as Python does", { timeout: 15000 }, async () => {
    // node-opcua's backoff library throws unless maxDelay > initialDelay, so
    // 100..100 used to fail every connect at once, plant up or not, with a
    // message about backoff delays rather than about the plant.
    assert.deepEqual(
      connectionStrategy({ initialDelay: 100, maxDelay: 100, maxRetry: 2, sessionTimeout: 60000 }),
      { initialDelay: 100, maxDelay: 101, maxRetry: 2 }
    );
    assert.deepEqual(
      connectionStrategy({ initialDelay: 500, maxDelay: 100, maxRetry: -1, sessionTimeout: 60000 }),
      { initialDelay: 100, maxDelay: 101, maxRetry: 4 }
    );
    const connection = new OpcuaConnection(REFUSING, undefined, {
      initialDelay: 100,
      maxDelay: 100,
      maxRetry: 1,
      sessionTimeout: 60000,
    });
    await quietly(() => assert.rejects(connection.connect(), /ECONNREFUSED/));
  });

  it("is abandoned by close() rather than waited out", { timeout: 15000 }, async () => {
    // A 5s backoff with -1 is a 20s round; shutting down must not take that long.
    const connection = new OpcuaConnection(REFUSING, undefined, {
      initialDelay: 5000,
      maxDelay: 5000,
      maxRetry: -1,
      sessionTimeout: 60000,
    });
    await quietly(async () => {
      const round = connection.connect().then(
        () => "connected",
        () => "rejected"
      );
      await new Promise((resolve) => setTimeout(resolve, 300));
      assert.equal(connection.connecting, true);

      const began = Date.now();
      await connection.close();
      assert.equal(await round, "rejected");
      assert.ok(Date.now() - began < 3000, `close() took ${Date.now() - began}ms`);
      assert.equal(connection.connecting, false);
      // And nothing starts again behind a server that is shutting down.
      await assert.rejects(connection.connect(), /shutting down/);
    });
  });
});

describe("requests served while the warm-up is still running", () => {
  /** An `OpcuaTools` whose warm-up never finishes on its own. */
  function stalled(waitMs) {
    const conn = new OpcuaConnection(REFUSING);
    let release;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    // A round that is in flight until the test releases it, as far as the
    // connection's own bookkeeping is concerned.
    conn.open = () => pending;
    const tools = new OpcuaTools(conn, new ToolPolicy(parsePolicyConfig({})));
    tools.warmUpWaitMs = waitMs;
    return { tools, conn, release };
  }

  it("answers tools/list at once, whole, without waiting for it", { timeout: 10000 }, async () => {
    // #140: the catalogue does not depend on the connection, so there is nothing
    // to wait for — not even the bounded warm-up window tools/list used to sit
    // through.
    const { tools, release } = stalled(5000);
    tools.startWarmUp();

    const began = Date.now();
    const listed = await tools.listTools();
    assert.ok(Date.now() - began < 100, `tools/list took ${Date.now() - began}ms`);
    assert.deepEqual(
      listed.map((tool) => tool.name),
      tools.policy.visibleTools(CONTRACT.tools).map((tool) => tool.name)
    );
    release();
  });

  it("reports a round in flight instead of joining it", { timeout: 10000 }, async () => {
    const { tools, conn, release } = stalled(100);
    tools.startWarmUp();

    const began = Date.now();
    const result = await tools.callTool({ params: { name: "get_server_status", arguments: {} } });
    assert.ok(Date.now() - began < 2000, `get_server_status took ${Date.now() - began}ms`);
    assert.notEqual(result.isError, true);
    const status = result.structuredContent.result;
    assert.equal(status.connected, false);
    assert.equal(status.error, stillConnectingMessage(conn.endpointUrl, null));
    release();
  });

  it(
    "authorizes a tool call only once the round in flight has ended",
    { timeout: 10000 },
    async () => {
      // The policy resolves `nsu=` entries through the namespace mapping bound on
      // connect, and the audit record names the session: both must see the
      // connection the warm-up was making, not the absence of one.
      const { tools, conn, release } = stalled(100);
      const seen = [];
      const policy = tools.policy;
      policy.authorize = () => {
        seen.push(conn.connecting);
        throw new Error("denied by the test");
      };
      tools.startWarmUp();

      const call = tools.callTool({ params: { name: "list_subscriptions", arguments: {} } });
      await new Promise((resolve) => setTimeout(resolve, 200));
      assert.deepEqual(seen, [], "the call was authorized while the warm-up was connecting");

      release();
      const result = await call;
      assert.deepEqual(seen, [false]);
      assert.equal(result.isError, true);
    }
  );

  it(
    "has get_server_status wait for a warm-up that finishes inside the window",
    { timeout: 10000 },
    async () => {
      // Against a plant that is up the first status must be a connected one,
      // which is why the warm-up used to run before any request was served.
      const { tools, conn, release } = stalled(5000);
      let finished = false;
      tools.warmUp = async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        finished = true;
      };
      conn.withRetry = async () => {
        throw new Error("offline, as far as this test is concerned");
      };
      tools.startWarmUp();
      await tools.callTool({ params: { name: "get_server_status", arguments: {} } });
      assert.equal(finished, true);
      release();
    }
  );
});
