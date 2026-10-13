/** node-opcua history adapter owns native requests, values and continuation points. */
import { AggregateFunction, type ClientSession, type DataValue } from "node-opcua-client";
import type { HistoryPort, HistoryRecord } from "../application/history.js";
import { CONTRACT } from "../contract.js";
import { AdapterFailure, ContractRefusal, describeError } from "../errors.js";
import {
  aggregateDetails,
  aggregatePages,
  continues,
  rawDetails,
  readContinuation,
  releaseContinuationPoint,
} from "../history.js";
import { historyData, toHistoryRecords } from "../records.js";
export class NodeOpcuaHistoryPort implements HistoryPort {
  constructor(private readonly session: ClientSession) {}
  async raw(
    nodeId: string,
    start: Date | undefined,
    end: Date | undefined,
    wanted: number
  ): Promise<{ records: HistoryRecord[]; continued: boolean }> {
    try {
      const readings = await this.session.readHistoryValue([nodeId], start as any, end as any, {
        numValuesPerNode: wanted,
        returnBounds: CONTRACT.history.rawReturnBounds,
      });
      if (readings.length !== 1) throw new Error("Read history failed");
      const reading = readings[0];
      const values = historyData<DataValue>(reading, "Read history", "dataValues");
      const continued = continues(reading.continuationPoint);
      await releaseContinuationPoint(
        this.session,
        nodeId,
        reading.continuationPoint,
        rawDetails(start, end, wanted)
      );
      return { records: toHistoryRecords(values), continued };
    } catch (error) {
      if (error instanceof ContractRefusal) throw error;
      throw new AdapterFailure("history-raw", describeError(error), error);
    }
  }
  async aggregate(
    nodeId: string,
    start: Date,
    end: Date,
    name: string,
    interval: number
  ): Promise<HistoryRecord[]> {
    try {
      const aggregateType = AggregateFunction[name as keyof typeof AggregateFunction];
      const details = aggregateDetails(start, end, aggregateType, interval);
      const first = await this.session.readAggregateValue(
        { nodeId },
        start,
        end,
        aggregateType,
        interval,
        details.aggregateConfiguration
      );
      const values = await aggregatePages(
        first,
        (point) => readContinuation(this.session, nodeId, point, details),
        (point) => releaseContinuationPoint(this.session, nodeId, point, details)
      );
      return toHistoryRecords(values);
    } catch (error) {
      if (error instanceof ContractRefusal) throw error;
      throw new AdapterFailure("history-aggregate", describeError(error), error);
    }
  }
}
