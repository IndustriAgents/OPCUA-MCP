import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { CONTRACT } from "../build/contract.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";

const byName = new Map(CONTRACT.tools.map((tool) => [tool.name, tool]));
const control = new Set(["write_opcua_node", "write_multiple_opcua_nodes", "call_opcua_method"]);

function policy(env) {
  return new ToolPolicy(parsePolicyConfig(env));
}

function visible(subject) {
  return new Set(CONTRACT.tools.filter((tool) => subject.isVisible(tool)).map((tool) => tool.name));
}

describe("tool policy", () => {
  it("defaults to the fail-closed observe profile", () => {
    const subject = policy({});
    assert.equal(subject.config.profile, "observe");
    for (const name of [...control, "acknowledge_alarm"]) {
      assert.equal(visible(subject).has(name), false);
    }
  });

  it("requires security or an explicit lab override for full control", () => {
    for (const name of control)
      assert.equal(visible(policy({ OPCUA_PROFILE: "full" })).has(name), false);
    const subject = policy({
      OPCUA_PROFILE: "full",
      OPCUA_ALLOW_INSECURE_CONTROL: "true",
    });
    for (const name of control) assert.equal(visible(subject).has(name), true);
  });

  it("enforces operator node and method allowlists", () => {
    const subject = policy({
      OPCUA_PROFILE: "operator",
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=13",
      OPCUA_ALLOWED_METHODS: "ns=2;i=27|ns=2;i=28",
    });
    subject.authorize("write_opcua_node", { node_id: "ns=2;i=13" });
    subject.authorize("call_opcua_method", {
      object_node_id: "ns=2;i=27",
      method_node_id: "ns=2;i=28",
    });
    assert.throws(
      () => subject.authorize("write_opcua_node", { node_id: "ns=2;i=14" }),
      /not writable/
    );
    assert.throws(
      () =>
        subject.authorize("call_opcua_method", {
          object_node_id: "ns=2;i=27",
          method_node_id: "ns=2;i=29",
        }),
      /not allowed/
    );
  });

  it("hides operator control families that have no configured targets", () => {
    const base = { OPCUA_PROFILE: "operator", OPCUA_SECURITY_POLICY: "Basic256Sha256" };
    const writeOnly = visible(policy({ ...base, OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=13" }));
    assert.equal(writeOnly.has("write_opcua_node"), true);
    assert.equal(writeOnly.has("write_multiple_opcua_nodes"), true);
    assert.equal(writeOnly.has("call_opcua_method"), false);

    const methodOnly = visible(policy({ ...base, OPCUA_ALLOWED_METHODS: "ns=2;i=27|ns=2;i=28" }));
    assert.equal(methodOnly.has("write_opcua_node"), false);
    assert.equal(methodOnly.has("write_multiple_opcua_nodes"), false);
    assert.equal(methodOnly.has("call_opcua_method"), true);
  });

  it("rejects direct calls to tools hidden from tools/list", () => {
    const subject = policy({});
    assert.equal(subject.isVisible(byName.get("write_opcua_node")), false);
    assert.throws(
      () => subject.authorize("write_opcua_node", { node_id: "ns=2;i=13" }),
      /disabled by OPCUA_PROFILE=observe/
    );
  });

  it("validates profiles, booleans and tool names", () => {
    assert.throws(() => parsePolicyConfig({ OPCUA_PROFILE: "god-mode" }), /Invalid OPCUA_PROFILE/);
    assert.throws(
      () => parsePolicyConfig({ OPCUA_ALLOW_INSECURE_CONTROL: "maybe" }),
      /must be true or false/
    );
    assert.throws(() => parsePolicyConfig({ OPCUA_ALLOWED_TOOLS: "not_a_tool" }), /Unknown tool/);
  });
});
