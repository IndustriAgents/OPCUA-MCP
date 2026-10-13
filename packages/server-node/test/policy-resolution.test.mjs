// The policy resolved against an address space: browse paths (and their
// ambiguity), writable_subtrees and deny_read expanded within their caps, and
// what the check on connect then reports and binds.
//
// `policyFindings` is pinned by the shared table; what is pinned here is the
// resolving that produces its input, against `fake-address-space.mjs`, and what
// the policy then allows and refuses. End to end against the mock is
// tests/e2e/test_info_model_e2e.py.
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { CONTRACT } from "../build/contract.js";
import { NodeFactsCache } from "../build/node-facts.js";
import { NodeMetadata } from "../build/node-metadata.js";
import { UNSTATED } from "../build/operation-limits.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { checkPolicy, resolvePolicyPath } from "../build/policy-resolution.js";
import { fakeAddressSpace } from "./fake-address-space.mjs";

const ANALOG_ITEM = "ns=0;i=2368";
const PROPERTY = "ns=0;i=68";

const variable = (name, parent, type, access = 3) => ({
  class: "Variable",
  name,
  parent,
  type,
  attributes: {
    DataType: "ns=0;i=11",
    ValueRank: -1,
    AccessLevel: access,
    UserAccessLevel: access,
  },
});

const PLANT = {
  "ns=0;i=84": { class: "Object", name: "0:Root" },
  "ns=0;i=85": { class: "Object", name: "0:Objects", parent: "ns=0;i=84" },
  "ns=2;i=1": { class: "Object", name: "2:Plant", parent: "ns=0;i=85" },
  // Two children called Pump, in two namespaces.
  "ns=2;i=8": { class: "Object", name: "2:Pump", parent: "ns=2;i=1" },
  "ns=3;i=8": { class: "Object", name: "3:Pump", parent: "ns=2;i=1" },
  "ns=2;i=41": variable("2:Setpoint", "ns=2;i=8", "ns=0;i=63"),
  "ns=2;i=3": variable("2:Temperature", "ns=2;i=8", "ns=0;i=63", 1),
  // A line of analogue tags, one of which has a property that is a Variable too.
  "ns=2;i=112": { class: "Object", name: "2:Line1", parent: "ns=2;i=1" },
  "ns=2;i=113": variable("2:LineSpeed", "ns=2;i=112", ANALOG_ITEM),
  "ns=2;i=114": variable("0:EURange", "ns=2;i=113", PROPERTY),
  "ns=2;i=115": variable("2:LineTension", "ns=2;i=112", ANALOG_ITEM),
  "ns=2;i=117": variable("2:LineLabel", "ns=2;i=112", "ns=0;i=63"),
  // Something to hide.
  "ns=2;i=118": { class: "Object", name: "2:Recipes", parent: "ns=2;i=1" },
  "ns=2;i=119": variable("2:SecretRecipe", "ns=2;i=118", "ns=0;i=63"),
  // An empty folder, and one that will not browse below its root.
  "ns=2;i=150": { class: "Object", name: "2:Empty", parent: "ns=2;i=1" },
  "ns=2;i=160": { class: "Object", name: "2:Locked", parent: "ns=2;i=1" },
  "ns=2;i=161": {
    class: "Object",
    name: "2:Vault",
    parent: "ns=2;i=160",
    browseStatus: "BadUserAccessDenied",
  },
};

/** A folder with more children than deny_read may hide. */
function withHugeFolder(count) {
  const nodes = { ...PLANT, "ns=4;i=1": { class: "Object", name: "4:Huge", parent: "ns=2;i=1" } };
  for (let index = 0; index < count; index++) {
    nodes[`ns=4;s=n${index}`] = variable(`4:n${index}`, "ns=4;i=1", "ns=0;i=63");
  }
  return nodes;
}

function policyFrom(document, env = {}) {
  const path = join(mkdtempSync(join(tmpdir(), "opcua-resolution-")), "policy.json");
  writeFileSync(path, JSON.stringify({ version: 1, ...document }), "utf8");
  const policy = new ToolPolicy(parsePolicyConfig({ OPCUA_POLICY_FILE: path, ...env }));
  policy.bindNamespaces([
    "http://opcfoundation.org/UA/",
    "urn:x",
    "urn:plant",
    "urn:other",
    "urn:big",
  ]);
  return policy;
}

async function check(policy, nodes = PLANT) {
  const quiet = console.error;
  const printed = [];
  console.error = (line) => printed.push(String(line));
  try {
    const record = await checkPolicy({
      session: fakeAddressSpace(nodes),
      policy,
      facts: new NodeFactsCache(),
      metadata: new NodeMetadata(),
      serverLimits: UNSTATED,
      generation: 3,
    });
    return { record, printed };
  } finally {
    console.error = quiet;
  }
}

describe("browse-path entries", () => {
  const session = fakeAddressSpace(PLANT);

  it("resolve segment by segment from the Root folder", async () => {
    assert.deepEqual(await resolvePolicyPath(session, "/Objects/Plant/Line1/LineSpeed"), {
      node_id: "ns=2;i=113",
      reason: null,
      segment: null,
      parent: null,
    });
  });

  it("refuse a segment more than one child matches", async () => {
    assert.deepEqual(await resolvePolicyPath(session, "/Objects/Plant/Pump/Setpoint"), {
      node_id: null,
      reason: "pathAmbiguous",
      segment: "Pump",
      parent: "ns=2;i=1",
    });
  });

  it("take a namespace-qualified segment as the one it names", async () => {
    const resolved = await resolvePolicyPath(session, "/Objects/Plant/2:Pump/Setpoint");
    assert.equal(resolved.node_id, "ns=2;i=41");
  });

  it("say where a path stopped matching", async () => {
    assert.deepEqual(await resolvePolicyPath(session, "/Objects/Plant/Nowhere"), {
      node_id: null,
      reason: "unresolved",
      segment: "Nowhere",
      parent: "ns=2;i=1",
    });
  });
});

describe("the policy check on connect", () => {
  it("binds browse-path allowlist entries, and reports an ambiguous one", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: {
        writable_nodes: ["/Objects/Plant/2:Pump/Setpoint", "/Objects/Plant/Pump/Setpoint"],
      },
    });
    assert.equal(policy.isWritable("ns=2;i=41"), false, "nothing by path before resolution");
    const { record, printed } = await check(policy);
    assert.equal(policy.isWritable("ns=2;i=41"), true);
    assert.deepEqual(record.writable_nodes, ["ns=2;i=41"]);
    assert.equal(record.generation, 3);
    assert.equal(record.findings.length, 1);
    assert.match(record.findings[0].problem, /is ambiguous: more than one child of ns=2;i=1/);
    assert.ok(printed.includes(`WARNING: policy check: ${record.findings[0].problem}`));
  });

  it("expands a typed writable_subtrees rule into exactly the Variables of that type", async () => {
    // Typed by each node's own HasTypeDefinition, not the copy a browse reference
    // carries — which the fake leaves wrong on purpose, as python-opcua does
    // after a node is retyped.
    const policy = policyFrom({
      profile: "operator",
      control: {
        writable_subtrees: [{ root: "/Objects/Plant/Line1", type_definition: "i=2368", max: 40 }],
      },
    });
    const { record } = await check(policy);
    assert.deepEqual(record.writable_nodes, ["ns=2;i=113", "ns=2;i=115"]);
    assert.equal(policy.isWritable("ns=2;i=117"), false);
    assert.equal(policy.boundFor("ns=2;i=113").maximum, 40);
    assert.deepEqual(record.findings, []);
  });

  it("matches every Variable under the root when the rule names no type", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: { writable_subtrees: [{ root: "ns=2;i=112" }] },
    });
    const { record } = await check(policy);
    assert.deepEqual(record.writable_nodes, [
      "ns=2;i=113",
      "ns=2;i=114",
      "ns=2;i=115",
      "ns=2;i=117",
    ]);
  });

  it("ignores a rule that matches more than its max_nodes, and says so", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: { writable_subtrees: [{ root: "ns=2;i=112", max_nodes: 3 }] },
    });
    const { record } = await check(policy);
    assert.deepEqual(record.writable_nodes, []);
    assert.equal(
      record.findings[0].problem,
      `writable_subtrees rule ns=2;i=112 matches more than 3 nodes, so it was ignored and allows nothing. Narrow the root or the type_definition, or raise max_nodes.`
    );
  });

  it("reports a rule that matched nothing", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: { writable_subtrees: [{ root: "ns=2;i=150" }] },
    });
    const { record } = await check(policy);
    assert.match(record.findings[0].problem, /matched no Variables/);
  });

  it("lets an explicit writable_nodes entry's bound win over a subtree rule's", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: {
        writable_nodes: ["ns=2;i=113"],
        writable_subtrees: [{ root: "ns=2;i=112", type_definition: "i=2368", max: 40 }],
      },
    });
    await check(policy);
    assert.equal(policy.boundFor("ns=2;i=113"), null);
    assert.equal(policy.boundFor("ns=2;i=115").maximum, 40);
  });

  it("hides a deny_read subtree from every read, under every profile", async () => {
    const policy = policyFrom({ deny_read: ["/Objects/Plant/Recipes"] });
    assert.equal(policy.isReadDenied("ns=2;i=119"), false, "a path hides nothing unresolved");
    const { record } = await check(policy);
    assert.equal(record.read_denied, 2);
    assert.equal(record.read_policy_complete, true);
    assert.deepEqual(record.writable_nodes, [], "not operator");
    assert.equal(policy.isReadDenied("ns=2;i=119"), true);
    assert.throws(
      () =>
        policy.authorizeRead("read_opcua_nodes", { node_ids: ["ns=2;i=41", "ns=2;i=119"] }, true),
      { message: CONTRACT.errors.readDenied.replace("{node_id}", "ns=2;i=119") }
    );
    policy.authorizeRead("read_opcua_nodes", { node_ids: ["ns=2;i=41"] }, true);
  });

  it("refuses every guarded read when deny_read would hide too much", async () => {
    const limit = CONTRACT.policyCheck.maxDenyNodes;
    const policy = policyFrom({ deny_read: ["ns=4;i=1", "ns=2;i=118"] });
    const { record } = await check(policy, withHugeFolder(limit));
    assert.equal(record.read_policy_complete, false);
    // The cap is in total; the entry that crossed it is named and the walk stops.
    assert.deepEqual(record.findings, [
      {
        entry: "ns=4;i=1",
        problem: `deny_read entry ns=4;i=1 takes the read policy past ${limit} nodes, so every read is refused until deny_read is narrowed.`,
      },
    ]);
    assert.throws(
      () => policy.authorizeRead("read_opcua_nodes", { node_ids: ["ns=2;i=41"] }, true),
      (error) =>
        error.message.startsWith(
          "Reads are refused until the read policy (deny_read) is fully resolved: deny_read entry ns=4;i=1 takes"
        ) && error.message.includes("narrowed. get_server_status")
    );
    // Before the connection, only what is known is refused.
    policy.authorizeRead("read_opcua_nodes", { node_ids: ["ns=2;i=41"] }, false);
  });

  it("refuses every guarded read when a node under a deny_read entry will not browse", async () => {
    const policy = policyFrom({ deny_read: ["ns=2;i=160"] });
    const { record } = await check(policy);
    assert.equal(record.read_policy_complete, false);
    assert.equal(
      record.findings[0].problem,
      "deny_read entry ns=2;i=160 could not be resolved (BadUserAccessDenied), so every read is refused until it is."
    );
  });

  it("reports a deny_read entry the server does not have, which hides nothing", async () => {
    const policy = policyFrom({ deny_read: ["ns=2;i=999", "/Objects/Nowhere"] });
    const { record } = await check(policy);
    assert.equal(record.read_denied, 0);
    assert.equal(record.read_policy_complete, true);
    assert.deepEqual(
      record.findings.map((finding) => finding.entry),
      ["ns=2;i=999", "/Objects/Nowhere"]
    );
  });

  it("checks the writable allowlist against the nodes' own attributes", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: { writable_nodes: ["ns=2;i=3", "ns=2;i=1"] },
    });
    const { record } = await check(policy);
    assert.deepEqual(
      record.findings.map((finding) => finding.problem),
      [
        "ns=2;i=3 names ns=2;i=3, which is read-only on the server (AccessLevel), so every write to it will fail.",
        CONTRACT.policyCheck.messages.notVariable
          .replace("{entry}", "ns=2;i=1")
          .replace("{node_id}", "ns=2;i=1")
          .replace("{node_class}", "Object"),
      ]
    );
  });

  it("does not resolve or lint the control rules outside operator", async () => {
    const policy = policyFrom({
      profile: "full",
      control: {
        writable_nodes: ["/Objects/Nowhere"],
        writable_subtrees: [{ root: "ns=2;i=150" }],
        alarm_sources: ["/Objects/Nowhere"],
      },
    });
    const { record } = await check(policy);
    assert.deepEqual(record.findings, []);
    assert.deepEqual(record.writable_nodes, []);
  });

  it("starts closed again on a new session, until resolved on it", async () => {
    const policy = policyFrom({
      deny_read: ["ns=2;i=118"],
      profile: "operator",
      control: { writable_nodes: ["/Objects/Plant/Line1/LineSpeed"] },
    });
    await check(policy);
    assert.equal(policy.isWritable("ns=2;i=113"), true);
    policy.forgetResolution("not yet");
    assert.equal(policy.isWritable("ns=2;i=113"), false);
    // A plain deny entry still hides its own node meanwhile.
    assert.equal(policy.isReadDenied("ns=2;i=118"), true);
    assert.throws(
      () => policy.authorizeRead("browse_opcua_nodes", { node_id: "ns=2;i=41" }, true),
      /fully resolved: not yet\./
    );
  });
  it("refuses all control while a precondition target or method pair does not resolve", async () => {
    const policy = policyFrom({
      profile: "operator",
      control: {
        writable_nodes: ["ns=2;i=41"],
        preconditions: [
          {
            targets: ["ns=2;i=41", "/Objects/Plant/NoSuchSetpoint"],
            methods: [{ object_id: "ns=2;i=1", method_id: "/Objects/Plant/NoSuchMethod" }],
            require: [{ node: "ns=2;i=3", equals: true }],
          },
        ],
      },
    });
    // Before resolution, the path target names nothing: closed.
    assert.equal(policy.unresolvedPrecondition(), "/Objects/Plant/NoSuchSetpoint");
    const { record } = await check(policy);
    assert.equal(policy.unresolvedPrecondition(), "/Objects/Plant/NoSuchSetpoint");
    // Linted per precondition: targets, then method pairs, then requirement nodes.
    assert.deepEqual(
      record.findings.map((finding) => finding.entry),
      ["/Objects/Plant/NoSuchSetpoint", "ns=2;i=1|/Objects/Plant/NoSuchMethod"]
    );
    assert.equal(
      CONTRACT.errors.preconditionUnresolved.replace("{entry}", "/Objects/Plant/NoSuchSetpoint"),
      "The operator policy has an interlock (preconditions) on /Objects/Plant/NoSuchSetpoint, which does not resolve on this server, so no write or method call is sent until it does. get_server_status reports it under policy_check."
    );
  });

  it("names an unresolved method pair as written, and lets everything resolved through", async () => {
    const pair = policyFrom({
      profile: "operator",
      control: {
        preconditions: [
          {
            methods: [{ object_id: "nsu=urn:missing;i=1", method_id: "ns=2;i=28" }],
            require: [{ node: "ns=2;i=3", equals: true }],
          },
        ],
      },
    });
    assert.equal(pair.unresolvedPrecondition(), "nsu=urn:missing;i=1|ns=2;i=28");

    const resolved = policyFrom({
      profile: "operator",
      control: {
        preconditions: [
          { targets: ["/Objects/Plant/Line1/LineSpeed"], require: [{ node: "ns=2;i=3", min: 0 }] },
        ],
      },
    });
    await check(resolved);
    assert.equal(resolved.unresolvedPrecondition(), null);
  });

  it("does not hold control outside operator, where preconditions do not apply", () => {
    const policy = policyFrom({
      profile: "full",
      control: {
        preconditions: [
          { targets: ["/Objects/Nowhere"], require: [{ node: "ns=2;i=3", equals: 1 }] },
        ],
      },
    });
    assert.equal(policy.unresolvedPrecondition(), null);
  });
});
