// What an allowlisted node may be *written*, not only which node it is (#109, #110).
//
// Node identity was the whole of write authorization, and it is the weakest link
// in the safety story: a model that correctly identified the right setpoint and
// hallucinated `9999` instead of `99.9` was fully authorized. The variant codec
// range-checks integers and refuses a lossy Int64, but that is *type* safety —
// `9999` is a perfectly good Double.
//
// `tests/unit/test_value_bounds.py` is the Python half and drives the same
// shared table. `min`, `max` and `enum` are decidable from the call alone, which
// is what lets the policy layer refuse a write before a single byte reaches the
// plant; `maxChange` and the server's own `EURange` need a read and live in the
// write path.
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DataType, StatusCodes } from "node-opcua-client";

import { asNumber, formatNumber, pairsAt, ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { NodeMetadata, withinRange } from "../build/node-metadata.js";
import { CONTRACT } from "../build/contract.js";
import { checkEuRange, checkMaxChange } from "../build/tools.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "value-bounds.json"), "utf8")
);

const WRITE_GUARD = { array: "nodes", nodeIdField: "node_id", valueField: "value" };

/** A policy file on disk, because that is the only way to express a bound. */
function writePolicy(document) {
  const path = join(mkdtempSync(join(tmpdir(), "opcua-bounds-")), "policy.json");
  writeFileSync(path, JSON.stringify({ version: 1, ...document }), "utf8");
  return path;
}

function policyFromFixture() {
  const spec = FIXTURE.policy;
  const subject = new ToolPolicy(
    parsePolicyConfig({
      OPCUA_PROFILE: spec.profile,
      OPCUA_SECURITY_POLICY: spec.secure ? "Basic256Sha256" : "None",
      OPCUA_POLICY_FILE: writePolicy({ control: { writable_nodes: spec.writable_nodes } }),
    })
  );
  subject.bindNamespaces(["http://opcfoundation.org/UA/", "urn:one", "urn:plant"]);
  return subject;
}

describe("value bounds, from the shared table", () => {
  const subject = policyFromFixture();

  for (const testCase of FIXTURE.cases) {
    // The Python half asserts the same verdict and the same sentence.
    it(testCase.name, () => {
      const args = { nodes: [{ node_id: testCase.node_id, value: testCase.value }] };
      if (testCase.error === null) {
        subject.authorize("write_opcua_nodes", args);
        return;
      }
      assert.throws(
        () => subject.authorize("write_opcua_nodes", args),
        (error) => {
          assert.equal(error.message, testCase.error);
          return true;
        }
      );
    });
  }

  it("refuses a batch whole when one value in it is out of bounds", () => {
    // A batch that applied the acceptable half and refused the rest would leave
    // the plant in a state nobody asked for and no record of which half landed.
    assert.throws(() =>
      subject.authorize("write_opcua_nodes", {
        nodes: [
          { node_id: "ns=2;i=1", value: 1 },
          { node_id: "ns=2;i=2", value: 9999 },
        ],
      })
    );
  });
});

describe("a bound on a renumbered server", () => {
  // A bound that only matched one spelling of a node id would be a bound an
  // operator believes is in force and is not — and the whole point of the `nsu=`
  // form is that the index moves when the server restarts.
  it("follows the namespace URI, not the index", () => {
    const subject = new ToolPolicy(
      parsePolicyConfig({
        OPCUA_PROFILE: "operator",
        OPCUA_SECURITY_POLICY: "Basic256Sha256",
        OPCUA_POLICY_FILE: writePolicy({
          control: { writable_nodes: [{ node: "nsu=urn:plant;i=5", max: 100 }] },
        }),
      })
    );

    subject.bindNamespaces(["http://opcfoundation.org/UA/", "urn:other", "urn:plant"]);
    assert.throws(() =>
      subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=2;i=5", value: 500 }] })
    );

    subject.bindNamespaces(["http://opcfoundation.org/UA/", "urn:plant", "urn:other"]);
    assert.throws(() =>
      subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 500 }] })
    );
    subject.authorize("write_opcua_nodes", { nodes: [{ node_id: "ns=1;i=5", value: 50 }] });
  });
});

describe("how a policy file is read", () => {
  it("keeps a bare node id legal", () => {
    // Every policy file written before bounds existed means what it meant.
    const config = parsePolicyConfig({
      OPCUA_PROFILE: "operator",
      OPCUA_POLICY_FILE: writePolicy({
        control: { writable_nodes: ["ns=2;i=1", { node: "ns=2;i=2", max: 10 }] },
      }),
    });
    assert.deepEqual([...config.writableNodes].sort(), ["ns=2;i=1", "ns=2;i=2"]);
    assert.equal(config.valueBounds.get("ns=2;i=1").maximum, null);
    assert.equal(config.valueBounds.get("ns=2;i=2").maximum, 10);
  });

  // Loud, because the failure it prevents is silent: an operator who writes
  // "minimum" instead of "min" believes a bound is in force and none is.
  for (const [entry, reason] of [
    [{ node: "ns=2;i=1", minimum: 0 }, /Unknown key/],
    [{ min: 0 }, /must name a node/],
    [{ node: "ns=2;i=1", min: "cold" }, /non-numeric min/],
    [{ node: "ns=2;i=1", min: 10, max: 0 }, /min above max/],
    [{ node: "ns=2;i=1", enum: [] }, /empty or non-list enum/],
    [{ node: "ns=2;i=1", max_change: -1 }, /negative max_change/],
    [42, /must be a node id or an object/],
  ]) {
    it(`refuses ${JSON.stringify(entry)} at startup`, () => {
      assert.throws(
        () =>
          parsePolicyConfig({
            OPCUA_PROFILE: "operator",
            OPCUA_POLICY_FILE: writePolicy({ control: { writable_nodes: [entry] } }),
          }),
        reason
      );
    });
  }

  it("carries node ids only in the environment variable", () => {
    // A comma-separated list cannot express a bound, and does not pretend to.
    // It replaces the file's list outright rather than merging: a half-overridden
    // allowlist is the kind of thing nobody can reason about at three in the
    // morning.
    const config = parsePolicyConfig({
      OPCUA_PROFILE: "operator",
      OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=1,ns=2;i=2",
      OPCUA_POLICY_FILE: writePolicy({
        control: { writable_nodes: [{ node: "ns=2;i=9", max: 10 }] },
      }),
    });
    assert.deepEqual([...config.writableNodes].sort(), ["ns=2;i=1", "ns=2;i=2"]);
  });

  it("refuses out-of-range writes unless the operator says otherwise", () => {
    // Default-on, because it is the only value bound that exists on a deployment
    // with no policy file at all, and the plant is the one who set it.
    assert.equal(parsePolicyConfig({}).allowOutOfRangeWrites, false);
    assert.equal(
      parsePolicyConfig({ OPCUA_ALLOW_OUT_OF_RANGE_WRITES: "true" }).allowOutOfRangeWrites,
      true
    );
  });
});

describe("the helpers the two runtimes share", () => {
  for (const [value, expected] of [
    [42, 42],
    [42.5, 42.5],
    ["42.5", 42.5],
    ["  42.5  ", 42.5],
    ["", null],
    ["warm", null],
    [true, null],
    [false, null],
    [null, null],
    [[1], null],
  ]) {
    it(`asNumber(${JSON.stringify(value)}) is ${expected}`, () => {
      assert.equal(asNumber(value), expected);
    });
  }

  // `format_number` in policy.py must agree character for character: the
  // refusals they build are compared by tests/e2e/test_runtime_differential.py.
  for (const [value, expected] of [
    [100, "100"],
    [-20.5, "-20.5"],
    [0.1, "0.1"],
    [Infinity, "infinity"],
    [-Infinity, "-infinity"],
  ]) {
    it(`formatNumber(${value}) is "${expected}"`, () => {
      assert.equal(formatNumber(value), expected);
    });
  }

  it("keeps each target with its own value", () => {
    // Flattening would authorise a value against the wrong node's bound.
    assert.deepEqual(
      pairsAt(
        {
          nodes: [
            { node_id: "ns=2;i=1", value: 1 },
            { node_id: "ns=2;i=2", value: 2 },
          ],
        },
        WRITE_GUARD
      ),
      [
        ["ns=2;i=1", 1],
        ["ns=2;i=2", 2],
      ]
    );
  });

  it("skips an element with no node id", () => {
    // Not a hole: the schema requires it and the allowlist has already refused it.
    assert.deepEqual(pairsAt({ nodes: [{ value: 1 }] }, WRITE_GUARD), []);
    assert.deepEqual(pairsAt({ nodes: "not a list" }, WRITE_GUARD), []);
  });

  it("treats a range as inclusive at both ends, as OPC UA's Range is", () => {
    const range = { low: 0, high: 100 };
    assert.equal(withinRange(range, 0), true);
    assert.equal(withinRange(range, 100), true);
    assert.equal(withinRange(range, 100.000001), false);
  });
});

// --- max_change, and the EURange the plant published -------------------------
//
// Both need a read, so they live in the write path rather than in the policy
// layer, and both refuse the whole batch before anything is sent.

const ANALOG = {
  unit: "°C",
  unit_description: "degree Celsius",
  eu_range: { low: 0, high: 150 },
  instrument_range: { low: -50, high: 250 },
};

/** Just enough of a node-opcua DataValue for the checks to read one. */
function dataValue(value, statusCode = StatusCodes.Good) {
  return { statusCode, value: { value, dataType: DataType.Double } };
}

describe("the EURange the plant published", () => {
  it("allows a value the plant says is normal", () => {
    checkEuRange("ns=2;i=90", 51.75, ANALOG);
    checkEuRange("ns=2;i=90", "51.75", ANALOG);
  });

  // The bound nobody had to type into a policy file, and the better one for it.
  it("refuses a value outside it", () => {
    assert.throws(
      () => checkEuRange("ns=2;i=90", 200, ANALOG),
      (error) => {
        assert.equal(
          error.message,
          "200 is outside the range node ns=2;i=90 accepts (0 to 150 °C), set by the " +
            "OPC UA server's own EURange. Nothing was written. Read the node to see where " +
            "it is now, or widen the bound if this is deliberate."
        );
        return true;
      }
    );
  });

  it("does not bound a node that published no range", () => {
    checkEuRange("ns=2;i=41", 999999, null);
    checkEuRange("ns=2;i=41", 999999, { ...ANALOG, eu_range: null });
  });

  it("leaves a non-numeric write to the codec", () => {
    // A range comparison says nothing useful about "warm"; the codec says it better.
    checkEuRange("ns=2;i=90", "warm", ANALOG);
    checkEuRange("ns=2;i=90", true, ANALOG);
  });
});

describe("max_change", () => {
  it("allows a move inside the limit, and exactly at it", () => {
    checkMaxChange("ns=2;i=90", 55, 10, dataValue(50));
    checkMaxChange("ns=2;i=90", 60, 10, dataValue(50));
  });

  it("refuses a move larger than the limit", () => {
    assert.throws(
      () => checkMaxChange("ns=2;i=90", 140, 10, dataValue(50)),
      (error) => {
        assert.equal(
          error.message,
          "Moving node ns=2;i=90 from 50 to 140 is a change of 90, and the operator policy " +
            "allows at most 10 in one write. Nothing was written. Make the move in steps if " +
            "it is deliberate."
        );
        return true;
      }
    );
  });

  it("measures the move in both directions", () => {
    assert.throws(() => checkMaxChange("ns=2;i=90", 10, 10, dataValue(50)));
  });

  // Refused rather than waved through: an unenforceable bound is not a bound.
  // This is also why a max_change on a write-only node cannot work — reading it
  // is exactly what such a node refuses.
  it("refuses when the node's current value cannot be read", () => {
    assert.throws(() => checkMaxChange("ns=2;i=90", 55, 10, undefined), /it could not be read/);
    assert.throws(
      () => checkMaxChange("ns=2;i=90", 55, 10, dataValue(null, StatusCodes.BadNotReadable)),
      /BadNotReadable/
    );
  });

  it("refuses an array, which has no single distance to have moved", () => {
    assert.throws(
      () => checkMaxChange("ns=2;i=90", [51, 52], 10, dataValue(50)),
      /only accepts a number/
    );
  });
});

// --- how the properties are actually fetched ---------------------------------

/** A session that records what was asked for, and answers with nothing.
 *
 * Enough to see the *shape* of the requests — how many, and how large — which is
 * the part a live server cannot be relied on to object to until it is a real
 * plant with a real operational limit.
 */
function recordingSession({ fail = null } = {}) {
  const state = { translateSizes: [], readSizes: [], fail };
  return {
    state,
    async translateBrowsePath(paths) {
      if (state.fail) throw state.fail;
      state.translateSizes.push(paths.length);
      return paths.map(() => ({ statusCode: StatusCodes.BadNoMatch, targets: [] }));
    },
    async read(nodesToRead) {
      state.readSizes.push(nodesToRead.length);
      return [];
    },
  };
}

describe("fetching a node's properties", () => {
  // MaxNodesPerTranslateBrowsePathsToNodeIds is an operational limit servers
  // publish and enforce. Nothing in the mock does, which is exactly why this is
  // asserted on the request shape rather than left to an end-to-end test to
  // discover against someone's real plant.
  it("splits a large batch into requests a server will accept", async () => {
    const session = recordingSession();
    const nodeIds = Array.from({ length: 500 }, (_, index) => `ns=2;i=${index}`);
    await new NodeMetadata().forNodes(session, nodeIds);

    const total = session.state.translateSizes.reduce((sum, size) => sum + size, 0);
    assert.equal(total, 1500);
    assert.ok(Math.max(...session.state.translateSizes) <= CONTRACT.analog.maxPropertiesPerRequest);
    assert.equal(session.state.translateSizes.length, 5);
  });

  it("stops asking a server that cannot answer the question at all", async () => {
    // A server without TranslateBrowsePathsToNodeIds will not learn to have it.
    // Asking on every read would cost a round trip and a line of stderr each
    // time, for the whole life of the session.
    const session = recordingSession({ fail: new Error("BadServiceUnsupported") });
    const metadata = new NodeMetadata();

    assert.equal((await metadata.forNodes(session, ["ns=2;i=1"])).get("ns=2;i=1"), null);
    assert.equal((await metadata.forNodes(session, ["ns=2;i=2"])).get("ns=2;i=2"), null);
    assert.deepEqual(session.state.translateSizes, [], "the second call reached the server");

    // And a new session starts the question over: it may be a different server.
    metadata.forget();
    session.state.fail = null;
    await metadata.forNodes(session, ["ns=2;i=3"]);
    assert.deepEqual(session.state.translateSizes, [3]);
  });

  it("does not remember a connection failure as an answer", async () => {
    // One bad moment must not leave a whole session's readings unqualified.
    const session = recordingSession({ fail: new Error("ECONNRESET") });
    const metadata = new NodeMetadata();

    assert.equal((await metadata.forNodes(session, ["ns=2;i=1"])).get("ns=2;i=1"), null);
    session.state.fail = null;
    await metadata.forNodes(session, ["ns=2;i=1"]);

    assert.deepEqual(session.state.translateSizes, [3], "the retry never happened");
  });
});
