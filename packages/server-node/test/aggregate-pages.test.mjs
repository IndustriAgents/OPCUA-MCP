import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { StatusCodes } from "node-opcua-client";
import {
  aggregatePages,
  aggregateDetails,
  readContinuation,
  releaseContinuationPoint,
} from "../build/history.js";
import { message } from "../build/errors.js";

const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/aggregate-pages.json", import.meta.url), "utf8")
).cases;
const result = (page) => ({
  continuationPoint: page.point ? Buffer.from(page.point) : null,
  statusCode: StatusCodes[page.status ?? "Good"],
  historyData: page.values ? { dataValues: page.values } : null,
});
for (const c of cases) {
  test(`aggregate pagination: ${c.name}`, async () => {
    const reads = [],
      released = [];
    let cursor = 1;
    const run = () =>
      aggregatePages(
        result(c.pages[0]),
        async (point) => {
          reads.push(point.toString());
          const page = c.pages[cursor++];
          if (page.throw) throw new Error(page.throw);
          return result(page);
        },
        async (point) => {
          released.push(point.toString());
          if (c.releaseThrows) throw new Error("release failed");
        },
        c.limit ?? 5000
      );
    if (c.expected) assert.deepEqual(await run(), c.expected);
    else {
      const expected = c.errorText ?? message(c.error, { limit: c.limit ?? 5000 });
      await assert.rejects(run, (error) => error.message === expected);
    }
    assert.deepEqual(reads, c.reads);
    assert.deepEqual(released, c.released);
  });
}

test("resume and release keep the original query and session", async () => {
  const requests = [];
  const details = aggregateDetails(new Date("2026-01-01"), new Date("2026-01-02"), 2342, 60000);
  const response = result({ values: [1] });
  const session = {
    async historyRead(request) {
      requests.push(request);
      return { results: [response] };
    },
  };
  assert.equal(await readContinuation(session, "ns=2;i=7", Buffer.from("held"), details), response);
  await releaseContinuationPoint(session, "ns=2;i=7", Buffer.from("held"), details);
  for (const [index, request] of requests.entries()) {
    assert.equal(request.historyReadDetails, details);
    assert.equal(request.nodesToRead[0].nodeId.toString(), "ns=2;i=7");
    assert.equal(request.nodesToRead[0].continuationPoint.toString(), "held");
    assert.equal(request.releaseContinuationPoints, Boolean(index));
    assert.equal(request.timestampsToReturn, 2);
  }
});

test("history tool reports completed server pages", async () => {
  const { ProtocolFeatureHandlers } = await import("../build/protocol/feature-handlers.js");
  const { DataValue, DataType, AggregateFunction } = await import("node-opcua-client");
  const queries = [];
  const page = (value, point) => ({
    ...result({ values: [new DataValue({ value: { dataType: DataType.Double, value } })] }),
    continuationPoint: point,
  });
  const session = {
    async readAggregateValue(node, start, end, type, interval, configuration) {
      queries.push({ node, start, end, type, interval, configuration });
      return page(1, Buffer.from("held"));
    },
    async historyRead(request) {
      assert.equal(request.releaseContinuationPoints, false);
      const first = queries[0],
        details = request.historyReadDetails;
      assert.equal(details.startTime.getTime(), first.start.getTime());
      assert.equal(details.endTime.getTime(), first.end.getTime());
      assert.equal(details.processingInterval, first.interval);
      assert.equal(details.aggregateType[0].value, AggregateFunction.Average);
      assert.equal(details.aggregateConfiguration, first.configuration);
      assert.equal(request.nodesToRead[0].continuationPoint.toString(), "held");
      return { results: [page(2, null)] };
    },
  };
  const tools = Object.create(ProtocolFeatureHandlers.prototype);
  tools.runtime = { conn: { session }, capabilities: { aggregateFunctions: ["Average"] } };
  tools.requireSession = () => session;
  const answer = await tools.readOpcuaHistory({
    nodeId: "ns=2;i=7",
    start: "2026-01-01T00:00:00Z",
    end: "2026-01-01T00:02:00Z",
    aggregateFunction: "Average",
    processingInterval: 60000,
    numValues: 0,
  });
  assert.deepEqual(
    answer.structuredContent.result.map((record) => record.value),
    [1, 2]
  );
  assert.equal(answer.structuredContent.completeness.complete, true);
  assert.equal(answer.structuredContent.completeness.continuation, null);
});
