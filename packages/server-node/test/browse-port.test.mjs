import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { browseNodes } from "../build/application/browse.js";
import { NodeOpcuaBrowsePort } from "../build/adapters/opcua-browse.js";
import { AdapterFailure } from "../build/errors.js";
import { isConnectionError } from "../build/connection.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/browse-port.json", import.meta.url), "utf8")
);
class FakeBrowsePort {
  constructor(blocked = []) {
    this.blocked = blocked;
    this.enriched = [];
  }
  async children(nodeId) {
    if (this.blocked.includes(nodeId)) throw new Error("blocked");
    return structuredClone(fixture.graph[nodeId] ?? []);
  }
  async describe(nodeId, parentNodeId) {
    const ref = Object.values(fixture.graph)
      .flat()
      .find((r) => r.nodeId === nodeId) ?? {
      namespaceIndex: 2,
      name: "Plant",
      nodeClass: "Object",
    };
    return {
      node_id: nodeId,
      browse_name: `${ref.namespaceIndex}:${ref.name}`,
      node_class: ref.nodeClass,
      parent_node_id: parentNodeId,
      data_type: null,
      value: null,
      description: null,
      type_definition: null,
    };
  }
  async enrich(records, includeValues) {
    this.enriched.push(records.map((r) => r.node_id));
    for (const record of records) {
      record.type_definition = "MockType";
      if (includeValues && record.node_class === "Variable")
        Object.assign(record, { value: 41.5, data_type: "Double", description: "Température °C" });
    }
  }
}
for (const c of fixture.cases)
  it(c.name, async () => {
    const before = JSON.stringify(fixture),
      port = new FakeBrowsePort(c.blocked);
    if (c.error) {
      await assert.rejects(
        () => browseNodes(port, c.request),
        (e) => e.message === c.error
      );
      assert.deepEqual(port.enriched, []);
    } else {
      const actual = await browseNodes(port, c.request);
      assert.deepEqual(
        actual.result.nodes.map((r) => r.node_id),
        c.nodeIds
      );
      assert.equal(actual.result.inspected, c.inspected);
      assert.equal(actual.result.truncated, c.truncated);
      assert.deepEqual(actual.completeness.reasons, c.reasons);
      assert.equal(actual.completeness.complete, c.reasons.length === 0);
      assert.equal(actual.completeness.returned, c.nodeIds.length);
      assert.deepEqual(port.enriched, [c.nodeIds]);
      if (c.request.includeValues)
        assert.equal(actual.result.nodes[0].description, "Température °C");
    }
    assert.equal(JSON.stringify(fixture), before);
  });
it("native failures retain their retry cause", async () => {
  const cause = Object.assign(new Error("native timeout"), { code: "ETIMEDOUT" });
  const port = new NodeOpcuaBrowsePort(
    {
      browse: async () => {
        throw cause;
      },
    },
    async () => ({})
  );
  await assert.rejects(
    () => port.children("ns=2;i=1"),
    (error) => {
      assert.ok(error instanceof AdapterFailure);
      assert.equal(error.cause, cause);
      assert.equal(isConnectionError(error), true);
      return true;
    }
  );
});
