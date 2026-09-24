import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { CONTRACT } from "../build/contract.js";
import { ToolPolicy, describePolicy, parsePolicyConfig } from "../build/policy.js";

const byName = new Map(CONTRACT.tools.map((tool) => [tool.name, tool]));
const control = new Set(["write_opcua_nodes", "call_opcua_method"]);

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
      OPCUA_SERVER_CERT: "/pki/server.pem",
      OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=13",
      OPCUA_ALLOWED_METHODS: "ns=2;i=27|ns=2;i=28",
    });
    subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=2;i=13" }] });
    subject.authorize("call_opcua_method", {
      object_node_id: "ns=2;i=27",
      method_node_id: "ns=2;i=28",
    });
    assert.throws(
      () => subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=2;i=14" }] }),
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
    const base = {
      OPCUA_PROFILE: "operator",
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_SERVER_CERT: "/pki/server.pem",
    };
    const writeOnly = visible(policy({ ...base, OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=13" }));
    assert.equal(writeOnly.has("write_opcua_nodes"), true);
    assert.equal(writeOnly.has("call_opcua_method"), false);

    const methodOnly = visible(policy({ ...base, OPCUA_ALLOWED_METHODS: "ns=2;i=27|ns=2;i=28" }));
    assert.equal(methodOnly.has("write_opcua_nodes"), false);
    assert.equal(methodOnly.has("call_opcua_method"), true);
  });

  it("rejects direct calls to tools hidden from tools/list", () => {
    const subject = policy({});
    assert.equal(subject.isVisible(byName.get("write_opcua_nodes")), false);
    assert.throws(
      () => subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=2;i=13" }] }),
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

// --- contract-derived guards (the safety layer stops keying off tool names) ------

/** An operator profile on a secured channel, one writable node, one method. */
function operator(extra = {}) {
  return policy({
    OPCUA_PROFILE: "operator",
    OPCUA_SECURITY_POLICY: "Basic256Sha256",
    OPCUA_SERVER_CERT: "/pki/server.pem",
    OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=13",
    OPCUA_ALLOWED_METHODS: "ns=2;i=1|ns=2;i=2",
    ...extra,
  });
}

describe("contract-derived policy guards", () => {
  it("every control tool in the contract declares a guard", () => {
    // A control tool with no guard is denied at runtime, so forgetting one is
    // safe — but it is also invisible, and the author would find out by way of a
    // tool that mysteriously does nothing. Failing here says so at the moment
    // the contract is edited.
    for (const tool of CONTRACT.tools) {
      if (tool.accessClass !== "control" && tool.accessClass !== "alarm-action") continue;
      assert.ok(tool.guard, `${tool.name} declares no guard`);
      const authorises = ["nodeIdPaths", "methodPaths", "flag"].some((key) => key in tool.guard);
      assert.ok(authorises, `${tool.name} has a guard that authorises nothing`);
    }
  });

  it("denies a control tool that declares no guard", () => {
    // The fail-open this replaced: `classVisible` ended in
    // `return config.writableNodes.size > 0`, so *any* unrecognised control tool
    // became visible as soon as one node was writable — and `authorize`'s
    // if/else chain then checked nothing at all.
    const invented = {
      name: "reboot_the_plc",
      accessClass: "control",
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false },
    };
    assert.equal(operator().isVisible(invented), false);
    // ...and `full`, which skips allowlists, still refuses it.
    const unlocked = policy({ OPCUA_PROFILE: "full", OPCUA_ALLOW_INSECURE_CONTROL: "true" });
    assert.equal(unlocked.isVisible(invented), false);
  });

  it("denies an access class the contract spelled wrong", () => {
    const typo = {
      name: "write_something",
      accessClass: "controll",
      guard: { nodeIdPaths: ["node_id"] },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true },
    };
    assert.equal(operator().isVisible(typo), false);
    const unlocked = policy({ OPCUA_PROFILE: "full", OPCUA_ALLOW_INSECURE_CONTROL: "true" });
    assert.equal(unlocked.isVisible(typo), false);
  });

  it("denies when a guard path selects nothing", () => {
    // A write whose target cannot be located is a write whose target cannot be
    // checked.
    const subject = operator();
    assert.throws(
      () => subject.authorize("write_opcua_nodes", { nodes: [{ value: 1 }] }),
      /requires nodes\.node_id/
    );
    assert.throws(() => subject.authorize("write_opcua_nodes", {}), /requires nodes\.node_id/);
  });

  it("rejects the whole batch when one target is forbidden", () => {
    assert.throws(
      () =>
        operator().authorize("write_opcua_nodes", {
          nodes: [{ node_id: "ns=2;i=13" }, { node_id: "ns=2;i=99" }],
        }),
      /ns=2;i=99 is not writable/
    );
  });
});

describe("node ids in the allowlist", () => {
  const pairs = [
    ["i=2253", "ns=0;i=2253"],
    ["ns=0;i=2253", "i=2253"],
    ["ns=2;i=13", " ns=2;i=13 "],
    [" ns=2;i=13 ", "ns=2;i=13"],
  ];

  for (const [allowed, requested] of pairs) {
    it(`matches ${JSON.stringify(allowed)} against ${JSON.stringify(requested)}`, () => {
      // The old matching was raw set membership on untrimmed strings, so an
      // entry written one way silently never matched a request written the
      // other — a denial, which is safe, but indistinguishable from a policy
      // mistake.
      const subject = operator({ OPCUA_ALLOWED_WRITE_NODES: allowed });
      subject.authorize("write_opcua_nodes", { nodes: [{ node_id: requested, value: 1 }] });
    });
  }

  it("canonicalises both sides of a method pair", () => {
    const subject = operator({ OPCUA_ALLOWED_METHODS: "i=1|i=2" });
    subject.authorize("call_opcua_method", {
      object_node_id: "ns=0;i=1",
      method_node_id: "ns=0;i=2",
    });
  });

  it("treats the same method under a different object as a different operation", () => {
    assert.throws(
      () =>
        operator().authorize("call_opcua_method", {
          object_node_id: "ns=2;i=9",
          method_node_id: "ns=2;i=2",
        }),
      /not allowed by the operator policy/
    );
  });
});

describe("namespace-URI allowlists", () => {
  const NAMESPACES = ["http://opcfoundation.org/UA/", "urn:plant:line-a"];

  it("authorises the node at that URI's index", () => {
    const subject = operator({ OPCUA_ALLOWED_WRITE_NODES: "nsu=urn:plant:line-a;i=5" });
    subject.bindNamespaces(NAMESPACES);
    subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 1 }] });
  });

  it("follows a reordered NamespaceArray", () => {
    // The failure the form exists to prevent: written `ns=1;i=5`, the allowlist
    // would go on authorising index 1 after a reorder — now a *different
    // physical node*, with nothing reporting anything wrong.
    const subject = operator({ OPCUA_ALLOWED_WRITE_NODES: "nsu=urn:plant:line-a;i=5" });
    subject.bindNamespaces(NAMESPACES);
    subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 1 }] });

    subject.bindNamespaces(["http://opcfoundation.org/UA/", "urn:other", "urn:plant:line-a"]);
    subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=2;i=5", value: 1 }] });
    assert.throws(
      () => subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 1 }] }),
      /not writable/
    );
  });

  it("authorises nothing for a URI the server does not publish", () => {
    const subject = operator({ OPCUA_ALLOWED_WRITE_NODES: "nsu=urn:not:here;i=5" });
    subject.bindNamespaces(NAMESPACES);
    for (const candidate of ["ns=0;i=5", "ns=1;i=5", "nsu=urn:not:here;i=5"]) {
      // The last one is the case that caught a real bug: an unresolvable id on
      // *both* sides used to share a placeholder and so compare equal.
      assert.throws(
        () => subject.authorize("write_opcua_nodes", { nodes: [{ node_id: candidate, value: 1 }] }),
        /not writable/,
        candidate
      );
    }
  });

  it("denies until the namespaces are known", () => {
    // Unknown is not empty. Resolving optimistically would authorise whatever
    // node happened to sit at the guessed index.
    const subject = operator({ OPCUA_ALLOWED_WRITE_NODES: "nsu=urn:plant:line-a;i=5" });
    assert.throws(
      () => subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 1 }] }),
      /not writable/
    );
  });
});

describe("the startup summary", () => {
  it("distinguishes a lab override from a secured channel", () => {
    // It used to print `insecure-control=enabled` for both, which is the
    // opposite of conspicuous: the one line an operator might scan said the
    // same thing whether control was properly secured or deliberately unlocked.
    // Every state of the gate is covered by tests/fixtures/control-gate.json.
    const secured = describePolicy(
      policy({ OPCUA_SECURITY_POLICY: "Basic256Sha256", OPCUA_SERVER_CERT: "/pki/server.pem" })
    );
    const override = describePolicy(policy({ OPCUA_ALLOW_INSECURE_CONTROL: "true" }));
    const blocked = describePolicy(policy({}));
    const unverified = describePolicy(policy({ OPCUA_SECURITY_POLICY: "Basic256Sha256" }));

    assert.match(secured, /control=secured/);
    assert.match(override, /control=INSECURE-OVERRIDE/);
    assert.match(blocked, /control=blocked/);
    // Encrypted is not verified, and the line says which one it is.
    assert.match(unverified, /control=blocked server-identity=unverified/);
    assert.equal(new Set([secured, override, blocked, unverified]).size, 4);
  });
});
