// Which type a browsed node is reported as (issue #120).
//
// A browse record named a node, its class and its parent — so every alarm,
// every pump and every folder came back as `Object`. `HasTypeDefinition` is
// what says which, and reading it is the whole of #120.
//
// The rule for *choosing* one is small and lives in a shared table, because a
// node's reported type is not an error anyone sees: it is a different string,
// and the two runtimes disagreeing about it would surface only as an agent
// reaching a different conclusion on one of them.
// `tests/unit/test_type_definitions.py` drives the same table.
//
// What is not here is the browse itself — that needs a real address space, and
// it is in `tests/e2e/test_type_definitions_e2e.py` against both mocks.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { ReferenceTypeIds } from "node-opcua-client";

import { typeDefinitionOf } from "../build/browse.js";
import { CONTRACT } from "../build/contract.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const CASES = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "type-definitions.json"), "utf8")
).cases;

describe("which type definition a node is reported as", () => {
  for (const testCase of CASES) {
    it(testCase.name, () => {
      assert.equal(
        typeDefinitionOf(testCase.is_good, testCase.browse_names),
        testCase.type_definition
      );
    });
  }

  it("uses the standard reference type", () => {
    // `ns=0;i=40` is HasTypeDefinition, fixed by OPC UA Part 3. It lives in the
    // contract rather than as a literal in each runtime for the same reason
    // every other node id does — but unlike most of them it is never seen in an
    // output, so a typo here would not produce a wrong answer. It would produce
    // *no* answer, on every node, with no error: a browse for a reference type
    // that does not exist succeeds and returns nothing.
    assert.equal(CONTRACT.traversal.hasTypeDefinitionNodeId, "ns=0;i=40");
    // node-opcua files it under ReferenceTypeIds, python-opcua under ObjectIds
    // — HasTypeDefinition is a ReferenceType, and python-opcua puts every
    // standard node in one enum. Same 40 either way, fixed by the spec, which
    // is what lets the Python half assert the same number.
    assert.equal(ReferenceTypeIds.HasTypeDefinition, 40);
  });

  it("chunks the batch below what one walk can return", () => {
    // `MaxNodesPerBrowse` is an operational limit a conformant server publishes
    // and enforces. The default walk returns up to 500 nodes, so an unchunked
    // request would be one a server is entitled to refuse — and refusing it
    // would lose every type in the result, silently, since this is best-effort.
    assert.ok(CONTRACT.traversal.maxTypeDefinitionsPerRequest < CONTRACT.traversal.defaultMaxNodes);
  });
});
