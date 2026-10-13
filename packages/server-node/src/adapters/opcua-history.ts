/** node-opcua history adapter owns native requests, values and continuation points. */
import { AggregateFunction, type ClientSession, type DataValue } from "node-opcua-client";
import type { HistoryPort, HistoryRecord } from "../application/history.js";
import { CONTRACT } from "../contract.js";
import { isConnectionError } from "../connection.js";
import { AdapterFailure, ContractRefusal, describeError, message } from "../errors.js";
import {
  aggregateDetails,
  aggregatePages,
  continues,
  rawDetails,
  readContinuation,
  releaseContinuationPoint,
} from "../history.js";
import { HISTORY_READ, factsGood, lacksBit, type NodeFactsCache } from "../node-facts.js";
import { historyData, toHistoryRecords } from "../records.js";
export class NodeOpcuaHistoryPort implements HistoryPort {
  constructor(
    private readonly session: ClientSession,
    private readonly facts: NodeFactsCache | null = null
  ) {}

  /** Why a history read failed, and a hint when the node says it keeps no history.
   *
   * Only after the read has failed, and never instead of it: AccessLevel's
   * HistoryRead bit is what the node advertises, and a server may keep history
   * for a node that does not say so — so it explains a failure, and does not
   * refuse a read up front. Not asked on a dead connection, which would only
   * fail again.
   */
  private async failure(operation: string, nodeId: string, error: unknown) {
    let reason = describeError(error);
    if (this.facts !== null && !isConnectionError(error)) {
      const facts = (await this.facts.forNodes(this.session, [nodeId])).get(nodeId) ?? null;
      if (factsGood(facts) && lacksBit(facts.access_level, HISTORY_READ))
        reason = `${reason} ${message("historyNotRecordedHint", { node_id: nodeId })}`;
    }
    return new AdapterFailure(operation, reason, error);
  }
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
      throw await this.failure("history-raw", nodeId, error);
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
      throw await this.failure("history-aggregate", nodeId, error);
    }
  }
}
