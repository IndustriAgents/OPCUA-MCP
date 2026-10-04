import { NodeOpcuaBrowsePort } from "../build/adapters/opcua-browse.js";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  AttributeIds,
  DataType,
  DataValue,
  StatusCodes,
  resolveNodeId,
  QualifiedName,
} from "node-opcua-client";
import { OpcuaConnection } from "../build/connection.js";
import { OpcuaTools } from "../build/tools.js";
import { alarmAction } from "../build/events.js";
const cases = (name) =>
  JSON.parse(readFileSync(new URL(`../../../tests/fixtures/${name}`, import.meta.url), "utf8"))
    .cases;
for (const c of cases("capability-probes.json")) {
  test(`aggregate discovery: ${c.name}`, async () => {
    const session = {
      async browse() {
        return {
          statusCode: StatusCodes.Good,
          references: c.references.map((r) => ({
            browseName: new QualifiedName({ name: r.name }),
            nodeId: resolveNodeId(r.nodeId),
          })),
        };
      },
    };
    const probe = await new OpcuaConnection().serverCapabilitiesAggregateFunctions(session);
    assert.deepEqual(probe.functions, c.expected);
    assert.equal(probe.support, c.expected.length ? "supported" : "not_supported");
  });
}
for (const c of cases("alarm-event-id.json")) {
  test(`alarm event ID: ${c.name}`, async () => {
    const calls = [],
      browses = [];
    const session = {
      async browse(request) {
        browses.push(request);
        return { statusCode: StatusCodes.Good, references: [] };
      },
      async call(request) {
        calls.push(request);
        return { statusCode: StatusCodes.Good };
      },
    };
    const run = () => alarmAction(session, "ns=2;i=7", c.eventId, "acknowledge");
    if (c.error) {
      await assert.rejects(run, (e) => e.message === c.error);
      assert.equal(calls.length, 0);
      assert.equal(browses.length, 0);
    } else {
      assert.equal(await run(), StatusCodes.Good);
      assert.equal(calls[0].inputArguments[0].value.toString("hex"), c.hex);
    }
  });
}
for (const [namespace, identifier, expected] of [
  [0, 11, "Double"],
  [2, 11, null],
  [0, 9999, null],
]) {
  test(`unreadable browse datatype: ${namespace}:${identifier}`, async () => {
    const record = {
      node_id: "ns=2;i=7",
      node_class: "Variable",
      value: null,
      data_type: null,
      description: null,
    };
    const session = {
      async read(requests) {
        return requests.map((r) =>
          r.attributeId === AttributeIds.Value
            ? new DataValue({ statusCode: StatusCodes.BadNotReadable })
            : r.attributeId === AttributeIds.DataType
              ? new DataValue({
                  value: {
                    dataType: DataType.NodeId,
                    value: resolveNodeId(`ns=${namespace};i=${identifier}`),
                  },
                })
              : new DataValue({
                  value: { dataType: DataType.LocalizedText, value: { text: "sensor" } },
                })
        );
      },
    };
    await new NodeOpcuaBrowsePort(session, async () => ({})).enrich([record], true);
    assert.equal(record.value, null);
    assert.equal(record.data_type, expected);
    assert.equal(record.description, "sensor");
  });
}
test("invalid aggregate name is a bare refusal", async () => {
  const tools = Object.create(OpcuaTools.prototype);
  tools.conn = { session: {} };
  tools.capabilities = { aggregateFunctions: ["Average", "Minimum"] };
  await assert.rejects(
    () =>
      tools.readOpcuaHistory({
        nodeId: "ns=2;i=7",
        start: "2026-01-01T00:00:00Z",
        aggregateFunction: "Bogus",
        processingInterval: 0,
        numValues: 0,
      }),
    (e) => e.message === "Invalid aggregate function. Supported: Average, Minimum"
  );
});
