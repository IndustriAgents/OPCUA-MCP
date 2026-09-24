// How a server's OperationLimits combine with this project's own (issue #139),
// driven by the table both runtimes share.
//
// `tests/unit/test_operation_limits.py` reads the same
// `tests/fixtures/operation-limits.json`. A server limit one runtime honours and
// the other does not is one plant request refused by one client and not the
// other.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { StatusCodes, VariableIds } from "node-opcua-client";

import { CONTRACT } from "../build/contract.js";
import { aggregateIntervals, chunked, effectiveLimit } from "../build/limits.js";
import { UNSTATED, readChunk, readOperationLimits, writeLimit } from "../build/operation-limits.js";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const table = JSON.parse(
  readFileSync(join(REPO_ROOT, "tests", "fixtures", "operation-limits.json"), "utf8")
);

describe("operation limits", () => {
  for (const testCase of table.effective) {
    it(testCase.name, () => {
      assert.equal(effectiveLimit(testCase.project, testCase.server), testCase.effective);
    });
  }

  for (const testCase of table.chunks) {
    it(`chunks: ${testCase.name}`, () => {
      const items = Array.from({ length: testCase.count }, (_, index) => index);
      const parts = chunked(items, testCase.size);
      assert.deepEqual(
        parts.map((part) => part.length),
        testCase.requests
      );
      // In order, with nothing lost or repeated: the records are put back by
      // position, so a chunking that reordered would misattribute every value.
      assert.deepEqual(parts.flat(), items);
    });
  }

  for (const testCase of table.aggregateIntervals) {
    it(`aggregate intervals: ${testCase.name}`, () => {
      assert.equal(
        aggregateIntervals(testCase.start_ms, testCase.end_ms, testCase.interval_ms),
        testCase.count
      );
    });
  }

  it("names the spec's nodes, as node-opcua numbers them", () => {
    const prefix = "Server_ServerCapabilities_OperationLimits_";
    assert.equal(
      CONTRACT.operationLimits.maxNodesPerRead,
      `ns=0;i=${VariableIds[`${prefix}MaxNodesPerRead`]}`
    );
    assert.equal(
      CONTRACT.operationLimits.maxNodesPerWrite,
      `ns=0;i=${VariableIds[`${prefix}MaxNodesPerWrite`]}`
    );
    assert.equal(
      CONTRACT.operationLimits.maxNodesPerBrowse,
      `ns=0;i=${VariableIds[`${prefix}MaxNodesPerBrowse`]}`
    );
    assert.equal(
      CONTRACT.operationLimits.maxNodesPerTranslateBrowsePathsToNodeIds,
      `ns=0;i=${VariableIds[`${prefix}MaxNodesPerTranslateBrowsePathsToNodeIds`]}`
    );
  });

  it("reads what the server states, and nothing where it states nothing", async () => {
    const session = {
      read: async () => [
        { statusCode: StatusCodes.Good, value: { value: 100 } },
        { statusCode: StatusCodes.Good, value: { value: 50 } },
        { statusCode: StatusCodes.BadNodeIdUnknown, value: { value: null } },
        { statusCode: StatusCodes.Good, value: { value: 0 } },
      ],
    };
    const limits = await readOperationLimits(session);
    assert.deepEqual(limits, {
      maxNodesPerRead: 100,
      maxNodesPerWrite: 50,
      maxNodesPerBrowse: null,
      maxNodesPerTranslateBrowsePathsToNodeIds: 0,
    });
    assert.equal(readChunk(limits), 100);
    assert.equal(writeLimit(limits), 50);
  });

  it("treats a server that cannot be asked as stating no limit", async () => {
    const session = {
      read: async () => {
        throw new Error("gone");
      },
    };
    assert.deepEqual(await readOperationLimits(session), UNSTATED);
    assert.equal(readChunk(UNSTATED), CONTRACT.limits.maxNodesPerRead);
    assert.equal(writeLimit(UNSTATED), CONTRACT.limits.maxNodesPerWrite);
  });
});
