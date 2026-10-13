// Reading what a node says about itself: the attributes, the DataType walk, the
// properties that name states — batched, chunked and cached for the session.
//
// The rules that consume these facts are pinned by the shared tables; what is
// pinned here is that the facts arrive as the tables assume, against an address
// space small enough to read in full (`fake-address-space.mjs`), and that the
// reading costs what node-facts.ts says it costs.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { NodeFactsCache } from "../build/node-facts.js";
import { fakeAddressSpace, text } from "./fake-address-space.mjs";

/** A Double read/write variable, the plainest thing there is. */
const DOUBLE = {
  class: "Variable",
  name: "2:ScratchDouble",
  attributes: {
    DataType: "ns=0;i=11",
    ValueRank: -1,
    ArrayDimensions: new Uint32Array(0),
    AccessLevel: 3,
    UserAccessLevel: 3,
  },
};

const PLANT = {
  "ns=2;i=41": DOUBLE,
  "ns=2;i=3": {
    ...DOUBLE,
    name: "2:Temperature",
    attributes: { ...DOUBLE.attributes, AccessLevel: 5, UserAccessLevel: 5 },
  },
  // An enumeration by DataType, whose states are on the DataType node.
  "ns=2;i=102": {
    ...DOUBLE,
    name: "2:MachineState",
    attributes: { ...DOUBLE.attributes, DataType: "ns=0;i=852" },
  },
  "ns=0;i=852": { class: "DataType", name: "0:ServerState", supertype: "ns=0;i=29" },
  "ns=0;i=7612": {
    class: "Variable",
    name: "0:EnumStrings",
    parent: "ns=0;i=852",
    attributes: { Value: [text("Running"), text("Failed")] },
  },
  // A MultiStateDiscrete: states on the variable.
  "ns=2;i=103": {
    ...DOUBLE,
    name: "2:ValveMode",
    attributes: { ...DOUBLE.attributes, DataType: "ns=0;i=7" },
  },
  "ns=2;i=104": {
    class: "Variable",
    name: "0:EnumStrings",
    parent: "ns=2;i=103",
    attributes: { Value: [text("Closed"), text(""), text("Auto")] },
  },
  // A TwoStateDiscrete: labels on the variable.
  "ns=2;i=105": {
    ...DOUBLE,
    name: "2:DoorLock",
    attributes: { ...DOUBLE.attributes, DataType: "ns=0;i=1" },
  },
  "ns=2;i=106": {
    class: "Variable",
    name: "0:TrueState",
    parent: "ns=2;i=105",
    attributes: { Value: text("Locked") },
  },
  "ns=2;i=107": {
    class: "Variable",
    name: "0:FalseState",
    parent: "ns=2;i=105",
    attributes: { Value: text("Unlocked") },
  },
  // EnumValues with a gap, and the Int64 node-opcua decodes as [high, low].
  "ns=2;i=130": {
    ...DOUBLE,
    name: "2:Speed",
    attributes: { ...DOUBLE.attributes, DataType: "ns=0;i=6" },
  },
  "ns=2;i=131": {
    class: "Variable",
    name: "0:EnumValues",
    parent: "ns=2;i=130",
    attributes: {
      Value: [
        { value: [0, 1], displayName: text("Low") },
        { value: [0, 5], displayName: text("High") },
      ],
    },
  },
  // A method that is switched off, on a folder whose type carries another.
  "ns=2;i=27": { class: "Object", name: "2:Methods", type: "ns=2;i=900" },
  "ns=2;i=109": {
    class: "Method",
    name: "2:DisabledMethod",
    parent: "ns=2;i=27",
    attributes: { Executable: false, UserExecutable: false },
  },
  "ns=2;i=900": { class: "ObjectType", name: "2:MethodsType", supertype: "ns=2;i=901" },
  "ns=2;i=901": { class: "ObjectType", name: "2:BaseMethodsType" },
  "ns=2;i=902": { class: "Method", name: "2:Inherited", parent: "ns=2;i=901" },
  "ns=2;i=903": { class: "Method", name: "2:Elsewhere" },
};

describe("node facts", () => {
  it("reads a Variable's attributes and resolves its type", async () => {
    const cache = new NodeFactsCache();
    const facts = (await cache.forNodes(fakeAddressSpace(PLANT), ["ns=2;i=41"])).get("ns=2;i=41");
    assert.deepEqual(facts, {
      status: "Good",
      node_class: "Variable",
      data_type: "Double",
      data_type_id: "ns=0;i=11",
      enumeration: false,
      value_rank: -1,
      // An empty ArrayDimensions says nothing, so it is null.
      array_dimensions: null,
      access_level: 3,
      user_access_level: 3,
      executable: null,
      user_executable: null,
      states: null,
      two_state: null,
    });
  });

  it("reports a node the server does not have by its status, and nothing else", async () => {
    const cache = new NodeFactsCache();
    const facts = (await cache.forNodes(fakeAddressSpace(PLANT), ["ns=2;i=999"])).get("ns=2;i=999");
    assert.equal(facts.status, "BadNodeIdUnknown");
    assert.equal(facts.node_class, null);
    assert.equal(facts.access_level, null);
  });

  it("finds an enumeration's states on its DataType", async () => {
    const cache = new NodeFactsCache();
    const facts = (await cache.forNodes(fakeAddressSpace(PLANT), ["ns=2;i=102"])).get("ns=2;i=102");
    assert.equal(facts.data_type, "Int32");
    assert.equal(facts.enumeration, true);
    assert.deepEqual(facts.states, [
      { value: 0, label: "Running" },
      { value: 1, label: "Failed" },
    ]);
  });

  it("prefers the variable's own EnumStrings, skipping empty labels", async () => {
    const cache = new NodeFactsCache();
    const facts = (await cache.forNodes(fakeAddressSpace(PLANT), ["ns=2;i=103"])).get("ns=2;i=103");
    assert.deepEqual(facts.states, [
      { value: 0, label: "Closed" },
      { value: 2, label: "Auto" },
    ]);
  });

  it("reads a two-state node's labels", async () => {
    const cache = new NodeFactsCache();
    const facts = (await cache.forNodes(fakeAddressSpace(PLANT), ["ns=2;i=105"])).get("ns=2;i=105");
    assert.deepEqual(facts.two_state, { true: "Locked", false: "Unlocked" });
    assert.equal(facts.states, null);
  });

  it("reads EnumValues with their own values, an Int64 decoded as [high, low]", async () => {
    const cache = new NodeFactsCache();
    const facts = (await cache.forNodes(fakeAddressSpace(PLANT), ["ns=2;i=130"])).get("ns=2;i=130");
    assert.deepEqual(facts.states, [
      { value: 1, label: "Low" },
      { value: 5, label: "High" },
    ]);
  });

  it("does not ask about states for a node whose type cannot have them", async () => {
    const session = fakeAddressSpace(PLANT);
    await new NodeFactsCache().forNodes(session, ["ns=2;i=41", "ns=2;i=3"]);
    assert.deepEqual(session.calls.translate, []);
  });

  it("asks once per session, and again only after forget()", async () => {
    const session = fakeAddressSpace(PLANT);
    const cache = new NodeFactsCache();
    await cache.forNodes(session, ["ns=2;i=41", "ns=2;i=103"]);
    const reads = session.calls.read.length;
    await cache.forNodes(session, ["ns=2;i=41", "ns=2;i=103"]);
    assert.equal(session.calls.read.length, reads, "a warm cache costs no round trip");
    cache.forget();
    await cache.forNodes(session, ["ns=2;i=41"]);
    assert.ok(session.calls.read.length > reads);
  });

  it("reads all the nodes' attributes in one Read, chunked by the server's limit", async () => {
    const session = fakeAddressSpace(PLANT);
    const cache = new NodeFactsCache();
    cache.serverLimits = { ...cache.serverLimits, maxNodesPerRead: 10 };
    await cache.forNodes(session, ["ns=2;i=41", "ns=2;i=3", "ns=2;i=999"]);
    // Three nodes, eight attributes each: 24 items in chunks of at most 10.
    assert.deepEqual(session.calls.read, [10, 10, 4]);
  });

  it("returns null facts, uncached, when the read fails, and never throws", async () => {
    const session = fakeAddressSpace(PLANT);
    const original = session.read;
    session.read = async () => {
      throw new Error("BadTimeout");
    };
    const cache = new NodeFactsCache();
    const quiet = console.error;
    console.error = () => {};
    try {
      assert.equal((await cache.forNodes(session, ["ns=2;i=41"])).get("ns=2;i=41"), null);
    } finally {
      console.error = quiet;
    }
    session.read = original;
    assert.equal((await cache.forNodes(session, ["ns=2;i=41"])).get("ns=2;i=41").status, "Good");
  });

  it("stops asking about states for the session when the server cannot translate", async () => {
    const session = fakeAddressSpace(PLANT);
    session.translateBrowsePath = async () => {
      throw new Error("BadServiceUnsupported");
    };
    const cache = new NodeFactsCache();
    const quiet = console.error;
    console.error = () => {};
    try {
      const first = (await cache.forNodes(session, ["ns=2;i=103"])).get("ns=2;i=103");
      assert.equal(first.states, null);
      assert.equal(first.data_type, "UInt32", "the attributes still arrive");
      let asked = false;
      session.translateBrowsePath = async () => {
        asked = true;
        return [];
      };
      await cache.forNodes(session, ["ns=2;i=105"]);
      assert.equal(asked, false);
    } finally {
      console.error = quiet;
    }
  });

  it("reads a method's Executable attributes", async () => {
    const facts = (
      await new NodeFactsCache().forNodes(fakeAddressSpace(PLANT), ["ns=2;i=109"])
    ).get("ns=2;i=109");
    assert.equal(facts.node_class, "Method");
    assert.equal(facts.executable, false);
    assert.equal(facts.user_executable, false);
  });

  it("finds a method on the object, on its type, or on a supertype", async () => {
    const cache = new NodeFactsCache();
    const session = fakeAddressSpace(PLANT);
    assert.equal(await cache.onObject(session, "ns=2;i=27", "ns=2;i=109"), true);
    assert.equal(await cache.onObject(session, "ns=2;i=27", "ns=2;i=902"), true);
    assert.equal(await cache.onObject(session, "ns=2;i=27", "ns=2;i=903"), false);
  });

  it("cannot tell, rather than says no, when a browse fails", async () => {
    const session = fakeAddressSpace(PLANT, { failBrowseOf: new Set(["ns=2;i=27"]) });
    const quiet = console.error;
    console.error = () => {};
    try {
      assert.equal(await new NodeFactsCache().onObject(session, "ns=2;i=27", "ns=2;i=109"), null);
    } finally {
      console.error = quiet;
    }
  });
});
