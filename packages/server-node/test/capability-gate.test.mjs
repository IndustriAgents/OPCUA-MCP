// A capability gates the call, never the catalogue (issue #140).
//
// The catalogue used to depend on what the connected server supported, so a
// server started while the plant was down advertised less than one started while
// it was up — and a client that listed once, as most do, kept the smaller list.
// These pin the replacement: every tool advertised whatever the answers are, the
// call decided against the answers for *its* session, and the refusal worded
// identically on both runtimes.
//
// `tests/unit/test_capability_gate.py` is the Python half and drives the same
// table, `tests/fixtures/capability-gate.json`. Needs no OPC UA server.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { StatusCodes } from "node-opcua-client";

import {
  CAPABILITY_NAMES,
  NOT_YET_ASKED,
  answersFrom,
  capabilityStatus,
  refusal,
  requirements,
  unasked,
  verdict,
} from "../build/capabilities.js";
import { OpcuaConnection } from "../build/connection.js";
import { CONTRACT } from "../build/contract.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { OpcuaTools } from "../build/tools.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "capability-gate.json"), "utf8")
);
const SPECS = new Map(CONTRACT.tools.map((tool) => [tool.name, tool]));

/** The fixture's answers, as this runtime holds them. */
function answers(raw) {
  const held = unasked();
  held.generation = raw.generation;
  held.checkedAt = raw.checked_at;
  held.support = { ...raw.support };
  if (raw.reasons !== null && raw.reasons !== undefined) held.reasons = { ...raw.reasons };
  held.aggregateFunctions = [...(raw.aggregate_functions ?? [])];
  return held;
}

describe("the shared capability table", () => {
  for (const kase of FIXTURE.decisions) {
    it(`decides: ${kase.name}`, () => {
      const decided = verdict(requirements(SPECS.get(kase.tool), kase.arguments), kase.support);
      assert.equal(decided.outcome, kase.expected.outcome);
      assert.deepEqual(decided.capabilities ?? [], kase.expected.capabilities);
    });
  }

  for (const kase of FIXTURE.messages) {
    it(`words: ${kase.name}`, () => {
      const text = refusal(kase.tool, kase.verdict, answers(kase.answers), kase.url);
      assert.equal(text, kase.expected);
      assert.ok(text.startsWith(`${kase.verdict.outcome}: `), text);
    });
  }

  for (const kase of FIXTURE.status) {
    it(`reports: ${kase.name}`, () => {
      assert.deepEqual(capabilityStatus(answers(kase.answers)), kase.expected);
    });
  }
});

describe("a capability probe", () => {
  /** A session whose one read answers, or fails, as the test says. */
  function session(outcome) {
    return {
      readVariableValue: async () => {
        if (outcome instanceof Error) throw outcome;
        return outcome;
      },
      browse: async () => {
        if (outcome instanceof Error) throw outcome;
        return outcome;
      },
    };
  }

  const conn = new OpcuaConnection("opc.tcp://127.0.0.1:1/none");

  it("reads true as supported", async () => {
    const probe = await conn.accessHistoryDataCapability(
      session({ statusCode: StatusCodes.Good, value: { value: true } })
    );
    assert.deepEqual(probe, { support: "supported", reason: null });
  });

  it("reads false, or a node the server does not have, as not supported", async () => {
    for (const answer of [
      { statusCode: StatusCodes.Good, value: { value: false } },
      { statusCode: StatusCodes.BadNodeIdUnknown, value: { value: null } },
    ]) {
      const probe = await conn.accessHistoryEventsCapability(session(answer));
      assert.equal(probe.support, "not_supported");
    }
  });

  it("reads a request that never completed as unknown, and says why", async () => {
    const probe = await conn.accessHistoryDataCapability(session(new Error("BadTimeout")));
    assert.deepEqual(probe, {
      support: "unknown",
      reason: "reading AccessHistoryDataCapability failed: BadTimeout",
      connectionLost: false,
    });
  });

  it("says when the session died under it, which is worth a rebuild", async () => {
    const probe = await conn.accessHistoryDataCapability(session(new Error("BadSessionIdInvalid")));
    assert.equal(probe.support, "unknown");
    assert.equal(probe.connectionLost, true);
  });

  it("reads an empty aggregate folder as not supported", async () => {
    const probe = await conn.serverCapabilitiesAggregateFunctions(
      session({ statusCode: StatusCodes.Good, references: [] })
    );
    assert.deepEqual(probe, { support: "not_supported", reason: null, functions: [] });
    const failed = await conn.serverCapabilitiesAggregateFunctions(session(new Error("closed")));
    assert.equal(failed.support, "unknown");
  });
});

describe("a session node-opcua re-created under us", () => {
  // node-opcua repairs a lost session on the same `ClientSession` object, so a
  // server restart never reaches `onSessionReplaced`. It must still be a new
  // generation — the one counter the audit record and the capability answers
  // both read — or answers from the old server would be trusted for the new one.
  function restoredAfter({ sessionId, startTime }) {
    const conn = new OpcuaConnection("opc.tcp://127.0.0.1:1/none");
    const session = {
      sessionId: { toString: () => sessionId },
      readVariableValue: async () => ({ value: { value: { startTime: new Date(startTime) } } }),
    };
    conn.session = session;
    conn.session_ = "before";
    conn.generation_ = 1;
    conn.serverSessionId = "ns=1;i=7";
    conn.serverStartTime = Date.parse("2026-09-24T10:00:00Z");
    const told = [];
    conn.onSessionRestored = async (restored) => told.push(restored);
    return { conn, session, told };
  }

  it("is a new generation when the server restarted, even under the same id", async () => {
    // python-opcua numbers sessions from a counter, so the first session after
    // a restart has the id the first one before it had.
    const { conn, session, told } = restoredAfter({
      sessionId: "ns=1;i=7",
      startTime: "2026-09-24T10:05:00Z",
    });
    await conn.restored(session);
    assert.equal(conn.sessionGeneration, 2);
    assert.notEqual(conn.sessionId, "before");
    assert.deepEqual(told, [session]);
  });

  it("is the same generation when it was only re-activated", async () => {
    const { conn, session, told } = restoredAfter({
      sessionId: "ns=1;i=7",
      startTime: "2026-09-24T10:00:00Z",
    });
    await conn.restored(session);
    assert.equal(conn.sessionGeneration, 1);
    assert.equal(conn.sessionId, "before");
    assert.deepEqual(told, []);
  });
});

/** An `OpcuaTools` on a connection that is up, on session `generation`, whose
 *  probes answer `fresh` and count how often they are asked. */
function harness({ generation, cached, fresh, lostOnce = false }) {
  const conn = new OpcuaConnection("opc.tcp://plc:4840");
  const state = { probes: 0, dispatched: [], rebuilds: 0, generation };
  conn.ensureConnection = async () => {};
  conn.session = {};
  Object.defineProperty(conn, "sessionGeneration", { get: () => state.generation });
  Object.defineProperty(conn, "sessionId", { get: () => `session-${state.generation}` });
  conn.reconnect = async () => {
    state.rebuilds += 1;
    state.generation += 1;
  };
  const probe = (name) => async () => {
    if (name === "history") state.probes += 1;
    if (lostOnce && state.rebuilds === 0) {
      return { support: "unknown", reason: "BadSessionIdInvalid", connectionLost: true };
    }
    return { support: fresh[name] ?? "not_supported", reason: null };
  };
  conn.accessHistoryDataCapability = probe("history");
  conn.accessHistoryEventsCapability = probe("historyEvents");
  conn.serverCapabilitiesAggregateFunctions = async () => ({
    ...(await probe("aggregate")()),
    functions: fresh.aggregate === "supported" ? ["Average"] : [],
  });

  const tools = new OpcuaTools(conn, new ToolPolicy(parsePolicyConfig({})));
  if (cached) {
    tools.capabilities = answersFrom(
      cached.generation,
      "2026-09-24T10:00:00.000Z",
      Object.fromEntries(
        CAPABILITY_NAMES.map((name) => [
          name,
          { support: cached[name] ?? "not_supported", reason: "cached" },
        ])
      ),
      []
    );
  }
  tools.dispatch = async (name) => {
    state.dispatched.push(name);
    return { content: [], structuredContent: { result: [] } };
  };
  return { tools, state };
}

function call(tools, name, args = {}) {
  return tools.callTool({ params: { name, arguments: args } });
}

describe("a gated call", () => {
  it("asks again when the answers are from an older session", async () => {
    // The server restarted without history: the old yes must not let it through.
    const { tools, state } = harness({
      generation: 2,
      cached: { generation: 1, history: "supported" },
      fresh: { history: "not_supported" },
    });
    const result = await call(tools, "read_opcua_history", { node_id: "ns=2;i=2" });
    assert.equal(state.probes, 1);
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /^capability_not_supported: read_opcua_history /);
    assert.deepEqual(state.dispatched, [], "a refused call must send nothing");
  });

  it("does not ask again when the answers are from this session", async () => {
    const { tools, state } = harness({
      generation: 2,
      cached: { generation: 2, history: "supported" },
      fresh: {},
    });
    const result = await call(tools, "read_opcua_history", { node_id: "ns=2;i=2" });
    assert.notEqual(result.isError, true);
    assert.equal(state.probes, 0);
    assert.deepEqual(state.dispatched, ["read_opcua_history"]);
  });

  it("asks again when the answer was unknown", async () => {
    const { tools, state } = harness({
      generation: 2,
      cached: { generation: 2, historyEvents: "unknown" },
      fresh: { historyEvents: "supported" },
    });
    const result = await call(tools, "read_event_history", {});
    assert.notEqual(result.isError, true);
    assert.equal(state.probes, 1);
  });

  it("confirms a cached no with the server before refusing", async () => {
    // A "no" from the cache would refuse without touching the network, so it
    // could never find out the server had come back with the feature.
    const { tools, state } = harness({
      generation: 2,
      cached: { generation: 2, history: "not_supported" },
      fresh: { history: "supported" },
    });
    const result = await call(tools, "read_opcua_history", { node_id: "ns=2;i=2" });
    assert.notEqual(result.isError, true);
    assert.equal(state.probes, 1);
  });

  it("rebuilds a session that died under the probe, and asks again", async () => {
    const { tools, state } = harness({
      generation: 2,
      cached: { generation: 2, history: "not_supported" },
      fresh: { history: "supported" },
      lostOnce: true,
    });
    const result = await call(tools, "read_opcua_history", { node_id: "ns=2;i=2" });
    assert.notEqual(result.isError, true, result.content[0]?.text);
    assert.equal(state.rebuilds, 1);
    assert.equal(state.probes, 2);
    assert.deepEqual(state.dispatched, ["read_opcua_history"]);
  });

  it("gates aggregate_function on aggregates, and a raw read on history", async () => {
    const { tools } = harness({
      generation: 1,
      cached: { generation: 1, history: "supported", aggregate: "not_supported" },
      fresh: {},
    });
    const raw = await call(tools, "read_opcua_history", { node_id: "ns=2;i=2" });
    assert.notEqual(raw.isError, true);
    const aggregated = await call(tools, "read_opcua_history", {
      node_id: "ns=2;i=2",
      start_time: "2026-09-24T00:00:00Z",
      aggregate_function: "Average",
    });
    assert.equal(aggregated.isError, true);
    assert.match(aggregated.content[0].text, /^capability_not_supported: .* aggregate functions/);
  });

  it("reports the answers it decided on in get_server_status", async () => {
    const { tools } = harness({
      generation: 3,
      cached: { generation: 3, history: "supported", historyEvents: "supported" },
      fresh: {},
    });
    tools.conn.withRetry = async () => {
      throw new Error("offline, as far as this test is concerned");
    };
    const result = await call(tools, "get_server_status");
    assert.deepEqual(result.structuredContent.result.capabilities, {
      session_generation: 3,
      checked_at: "2026-09-24T10:00:00.000Z",
      support: { history: "supported", historyEvents: "supported", aggregate: "not_supported" },
      aggregate_functions: [],
    });
  });
});

describe("the catalogue", () => {
  it("is the same whatever the server was found to support", async () => {
    // Nothing asked, everything supported, nothing supported. Before #140 the
    // first withheld both history tools and the last withheld
    // aggregate_function too.
    const catalogues = [];
    for (const cached of [
      null,
      { generation: 1, history: "supported", historyEvents: "supported", aggregate: "supported" },
      { generation: 1 },
    ]) {
      const { tools } = harness({ generation: 1, cached, fresh: {} });
      catalogues.push(JSON.stringify(await tools.listTools()));
    }
    assert.equal(catalogues[0], catalogues[1]);
    assert.equal(catalogues[0], catalogues[2]);
    const listed = JSON.parse(catalogues[0]);
    const history = listed.find((tool) => tool.name === "read_opcua_history");
    assert.deepEqual(history.inputSchema, SPECS.get("read_opcua_history").inputSchema);
  });

  it("names no capability as unknown for a reason nobody gave", () => {
    for (const name of CAPABILITY_NAMES) assert.equal(unasked().reasons[name], NOT_YET_ASKED);
  });
});
