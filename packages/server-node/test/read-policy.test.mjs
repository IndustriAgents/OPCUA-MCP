// The policy's newer surfaces that need no server: the read guard and deny_read
// before resolution, the namespace-URI form the audit trail records, and what
// writable_subtrees does to the catalogue.
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { CONTRACT } from "../build/contract.js";
import { ToolPolicy, parsePolicyConfig, valuesAt } from "../build/policy.js";

const SECURE = { OPCUA_SECURITY_POLICY: "Basic256Sha256", OPCUA_SERVER_CERT: "/pki/server.pem" };
const NAMESPACES = ["http://opcfoundation.org/UA/", "urn:x", "urn:plant"];

function policyFrom(document, env = {}) {
  const path = join(mkdtempSync(join(tmpdir(), "opcua-read-policy-")), "policy.json");
  writeFileSync(path, JSON.stringify({ version: 1, ...document }), "utf8");
  const policy = new ToolPolicy(parsePolicyConfig({ OPCUA_POLICY_FILE: path, ...env }));
  policy.bindNamespaces(NAMESPACES);
  return policy;
}

describe("valuesAt", () => {
  it("reads a trailing name[] as every string element of the list", () => {
    assert.deepEqual(valuesAt({ node_ids: ["ns=2;i=1", 7, null, "ns=2;i=2"] }, "node_ids[]"), [
      "ns=2;i=1",
      "ns=2;i=2",
    ]);
  });

  it("selects nothing from an argument that is not a list", () => {
    assert.deepEqual(valuesAt({ node_ids: "ns=2;i=1" }, "node_ids[]"), []);
    assert.deepEqual(valuesAt({}, "node_ids[]"), []);
  });

  it("still reads array[].field", () => {
    assert.deepEqual(
      valuesAt({ nodes: [{ node_id: "a" }, { node_id: "b" }, "c"] }, "nodes[].node_id"),
      ["a", "b"]
    );
  });
});

describe("the read guard", () => {
  it("is declared on every tool that reads nodes deny_read can name", () => {
    const guarded = Object.fromEntries(
      CONTRACT.tools
        .filter((tool) => tool.readGuard)
        .map((tool) => [tool.name, tool.readGuard.nodeIdPaths])
    );
    assert.deepEqual(guarded, {
      read_opcua_nodes: ["node_ids[]"],
      browse_opcua_nodes: ["node_id"],
      read_opcua_history: ["node_id"],
      subscribe_opcua_nodes: ["node_ids[]"],
    });
  });

  for (const profile of ["observe", "operator", "full"]) {
    it(`refuses a plain deny_read node before anything is sent, under ${profile}`, () => {
      const policy = policyFrom(
        { deny_read: ["ns=2;i=118"] },
        { OPCUA_PROFILE: profile, ...SECURE }
      );
      assert.throws(() => policy.authorize("read_opcua_nodes", { node_ids: ["ns=2;i=118"] }), {
        message: CONTRACT.errors.readDenied.replace("{node_id}", "ns=2;i=118"),
      });
      policy.authorize("read_opcua_nodes", { node_ids: ["ns=2;i=41"] });
    });
  }

  it("compares by node, not by spelling", () => {
    const policy = policyFrom({ deny_read: ["nsu=urn:plant;i=118"] });
    assert.throws(() => policy.authorize("read_opcua_history", { node_id: "ns=2;i=118" }));
  });

  it("leaves a tool with no read guard alone", () => {
    const policy = policyFrom({ deny_read: ["ns=2;i=118"] });
    policy.authorize("read_event_history", { node_id: "ns=2;i=118" });
  });

  it("refuses nothing it does not know about until the policy is resolved, when not strict", () => {
    const policy = policyFrom({ deny_read: ["/Objects/Plant/Recipes"] });
    policy.forgetResolution("not resolved");
    policy.authorize("read_opcua_nodes", { node_ids: ["ns=2;i=119"] });
    assert.throws(
      () => policy.authorizeRead("read_opcua_nodes", { node_ids: ["ns=2;i=41"] }, true),
      /fully resolved: not resolved\./
    );
  });
});

describe("the namespace-URI form", () => {
  const policy = policyFrom({});

  it("writes an index as the URI it stands for", () => {
    assert.equal(policy.uriForm("ns=2;i=41"), "nsu=urn:plant;i=41");
    assert.equal(policy.uriForm("i=85"), "nsu=http://opcfoundation.org/UA/;i=85");
    assert.equal(policy.uriForm("ns=2;s=a;b"), "nsu=urn:plant;s=a;b");
  });

  it("is null for an index the server did not report, or before it reported any", () => {
    assert.equal(policy.uriForm("ns=9;i=1"), null);
    assert.equal(policy.uriForm("nsu=urn:missing;i=1"), null);
    assert.equal(new ToolPolicy(parsePolicyConfig({})).uriForm("ns=2;i=41"), null);
  });
});

describe("writable_subtrees and the catalogue", () => {
  const write = CONTRACT.tools.find((tool) => tool.name === "write_opcua_nodes");

  it("offers write_opcua_nodes under operator when only a subtree rule is configured", () => {
    const policy = policyFrom(
      { profile: "operator", control: { writable_subtrees: [{ root: "ns=2;i=112" }] } },
      SECURE
    );
    assert.equal(policy.isVisible(write), true);
    // Configured, not resolved: nothing is writable until the rule is expanded.
    assert.equal(policy.isWritable("ns=2;i=113"), false);
  });

  it("names why the tool is not callable at all, for write_access", () => {
    const policy = policyFrom({});
    assert.equal(
      policy.toolRefusal("write_opcua_nodes"),
      CONTRACT.errors.toolDisabled
        .replace("{tool}", "write_opcua_nodes")
        .replace("{profile}", "observe")
    );
  });
});
