import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { CONTRACT } from "../build/contract.js";
import { OpcuaConnection } from "../build/connection.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { NodeMetadata } from "../build/node-metadata.js";
import { Admission, BoundedCache, BoundedRecords } from "../build/resources.js";
import { SubscriptionManager } from "../build/subscriptions.js";
import { NodeOpcuaMethodPort } from "../build/adapters/opcua-methods.js";
import { runControl } from "../build/infrastructure/control-context.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/architecture-safety.json", import.meta.url), "utf8")
);
const policy = () =>
  new ToolPolicy(
    parsePolicyConfig({
      OPCUA_PROFILE: "operator",
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_SERVER_CERT: "/pki/server.pem",
      OPCUA_ALLOWED_WRITE_NODES: fixture.namespaces.allowed,
      OPCUA_ALLOWED_METHODS: "ns=2;i=1|ns=2;i=2",
    })
  );

test("failed namespace refresh revokes previous URI permission", async () => {
  const subject = policy();
  subject.bindNamespaces(fixture.namespaces.initial);
  const args = { nodes: [{ node_id: fixture.namespaces.target, value: 1 }] };
  subject.authorize("write_opcua_nodes", args);
  const connection = new OpcuaConnection("opc.tcp://unused", subject);
  await connection.bindPolicyNamespaces({
    async readVariableValue() {
      throw new Error("timeout");
    },
  });
  assert.throws(() => subject.authorize("write_opcua_nodes", args));
});
for (const c of fixture.buffers)
  test(c.name, () => {
    const records = new BoundedRecords(c.capacity, c.bytes);
    records.push(...c.records);
    assert.deepEqual([...records], c.retained);
    assert.ok(records.byteCount <= c.bytes);
    records.splice(0, records.length);
    assert.equal(records.byteCount, 0);
  });
test("cache evicts oldest entries", () => {
  const c = fixture.cache,
    cache = new BoundedCache(c.capacity);
  for (const [key, value] of c.entries) cache.set(key, value);
  assert.deepEqual([...cache], c.retained);
});
test("pending subscription creation consumes capacity", async () => {
  const manager = new SubscriptionManager();
  manager.attach = async () => {};
  for (let i = 0; i < CONTRACT.limits.maxSubscriptions - 1; i++)
    await manager.subscribe({}, `ns=2;i=${i}`);
  let release;
  manager.attach = () =>
    new Promise((resolve) => {
      release = resolve;
    });
  const pending = manager.subscribe({}, "ns=2;i=999");
  try {
    await assert.rejects(manager.subscribe({}, "ns=2;i=1000"), /subscription/);
  } finally {
    release();
    await pending;
  }
  assert.equal(manager.list().length, CONTRACT.limits.maxSubscriptions);
});
test("control ranges do not reuse best-effort failures", async () => {
  const metadata = new NodeMetadata();
  metadata.read = async () => {
    throw new Error("unreadable");
  };
  assert.deepEqual(await metadata.forNodes({}, ["ns=2;i=5"]), new Map([["ns=2;i=5", null]]));
  await assert.rejects(metadata.forControl({}, ["ns=2;i=5"]), /Nothing was written/);
});
test("overload reserves diagnostics and releases capacity", async () => {
  const admission = new Admission(),
    releases = [];
  const pending = Array.from({ length: CONTRACT.limits.maxConcurrentCalls }, () =>
    admission.run("read_opcua_nodes", () => new Promise((resolve) => releases.push(resolve)))
  );
  try {
    await assert.rejects(
      admission.run("read_opcua_nodes", async () => {}),
      /Resource limit/
    );
    assert.equal(await admission.run("get_server_status", async () => "available"), "available");
  } finally {
    for (const release of releases) release();
    await Promise.all(pending);
  }
  await admission.run("read_opcua_nodes", async () => {});
});
test("session change during method preparation prevents native send", async () => {
  const c = fixture.control,
    sends = [];
  const port = new NodeOpcuaMethodPort({
    async call() {
      sends.push(1);
    },
  });
  const call = {
    name: c.tool,
    session: c.session,
    arguments: { object_node_id: c.object, method_node_id: c.method },
  };
  await assert.rejects(
    runControl(call, { connected: true, sessionId: "replacement" }, policy(), () =>
      port.call(c.object, c.method, [])
    ),
    /session changed/
  );
  assert.deepEqual(sends, []);
});

test("cache byte budget is released on clear", () => {
  const cache = new BoundedCache(10, 25);
  cache.set("a", "1234567890");
  cache.set("b", "1234567890");
  assert.deepEqual([...cache.keys()], ["b"]);
  assert.ok(cache.byteCount <= 25);
  cache.clear();
  assert.equal(cache.byteCount, 0);
});

test("late lookup cannot refill a cleared session cache", async () => {
  const { cacheGeneration } = await import("../build/resources.js");
  const cache = new BoundedCache();
  await cacheGeneration([cache], async () => {
    await Promise.resolve();
    cache.clear();
    cache.set("stale", "old session");
  });
  assert.equal(cache.size, 0);
  cache.set("fresh", "new session");
  assert.deepEqual([...cache.keys()], ["fresh"]);
});

test("uncertain audit retains the sending session and URI after failed recovery", async () => {
  const { AuditSink } = await import("../build/audit.js");
  const { OpcuaTools } = await import("../build/tools.js");
  const subject = policy();
  subject.bindNamespaces(fixture.namespaces.initial);
  const connection = new OpcuaConnection("opc.tcp://unused", subject);
  connection.session_ = "original";
  connection.generation_ = 1;
  connection.state = "connected";
  connection.session = {};
  connection.ensureConnection = async () => {};
  connection.reconnect = async () => {
    connection.session_ = "replacement";
    connection.generation_ = 2;
    subject.bindNamespaces(fixture.namespaces.reordered);
    throw new Error("endpoint still offline");
  };
  const sink = new AuditSink(),
    records = [];
  sink.write = (record) => records.push(record);
  const tools = new OpcuaTools(connection, subject, sink);
  tools.runtime.ensurePolicy = async () => {};
  tools.dispatch = async () => {
    throw Object.assign(new Error("reply lost"), { code: "ECONNRESET" });
  };
  const result = await tools.callTool({
    params: {
      name: "write_opcua_nodes",
      arguments: { nodes: [{ node_id: fixture.namespaces.target, value: 1 }] },
    },
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /whether it took effect is unknown/);
  assert.deepEqual(
    records.map((r) => r.decision),
    ["allowed", "uncertain"]
  );
  assert.deepEqual(
    records.map((r) => r.session),
    ["original", "original"]
  );
  assert.deepEqual(
    records.map((r) => r.session_generation),
    [1, 1]
  );
  assert.deepEqual(records[1].node_uris, [fixture.namespaces.allowed]);
});

test("a cancelled method request cannot send after preparation", async () => {
  const c = fixture.control,
    controller = new AbortController(),
    sends = [];
  const port = new NodeOpcuaMethodPort({
    async call() {
      sends.push(1);
    },
  });
  const call = {
    name: c.tool,
    session: c.session,
    arguments: { object_node_id: c.object, method_node_id: c.method },
  };
  controller.abort();
  await assert.rejects(
    runControl(
      call,
      { connected: true, sessionId: c.session },
      policy(),
      () => port.call(c.object, c.method, []),
      controller.signal
    ),
    /cancelled before dispatch/
  );
  assert.deepEqual(sends, []);
});

test("automatic session restoration refreshes URI authorization", async () => {
  const subject = policy();
  subject.bindNamespaces(fixture.namespaces.initial);
  const connection = new OpcuaConnection("opc.tcp://unused", subject);
  const session = {
    sessionId: { toString: () => "replacement-server-session" },
    async readVariableValue() {
      return { value: { value: fixture.namespaces.reordered } };
    },
  };
  connection.session = session;
  connection.serverSessionId = "old-server-session";
  connection.serverStartTime = 1;
  connection.session_ = "original";
  connection.generation_ = 1;
  connection.startTimeOf = async () => 2;
  await connection.restored(session);
  assert.equal(connection.sessionGeneration, 2);
  assert.throws(() =>
    subject.authorize("write_opcua_nodes", {
      nodes: [{ node_id: fixture.namespaces.target, value: 1 }],
    })
  );
  subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 1 }] });
});
