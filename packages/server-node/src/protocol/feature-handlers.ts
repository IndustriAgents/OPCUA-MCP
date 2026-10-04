import { NodeOpcuaEventPort } from "../adapters/opcua-events.js";
/** Thin MCP feature adapters over application services and instance-owned native ports. */
import type { NodeOpcuaRuntime } from "../adapters/opcua-runtime.js";

import {
  withNotice,
  historyResult,
  eventResult,
  objectResult,
  recordBlocks,
  type ServerStatusReport,
} from "../protocol/results.js";

import { subscribeNodes, unsubscribeNodes } from "../application/subscriptions.js";
import { NodeOpcuaSubscriptionPort } from "../adapters/opcua-subscriptions.js";
import { actOnAlarm, listAlarms } from "../application/alarms.js";
import { NodeOpcuaAlarmPort } from "../adapters/opcua-alarms.js";
import { subscribeEvents, readEvents, readEventHistory } from "../application/events.js";

import { readHistory } from "../application/history.js";
import { NodeOpcuaHistoryPort } from "../adapters/opcua-history.js";
import { writeNodes, type WriteRequest } from "../application/write.js";
import { NodeOpcuaWritePort } from "../adapters/opcua-write.js";
import { callMethod } from "../application/methods.js";
import { NodeOpcuaMethodPort } from "../adapters/opcua-methods.js";
import { browseNodes, type BrowseRequest } from "../application/browse.js";
import { NodeOpcuaBrowsePort } from "../adapters/opcua-browse.js";
import { readNodes } from "../application/read.js";
import { NodeOpcuaReadPort } from "../adapters/opcua-read.js";

import { capabilityStatus } from "../capabilities.js";

import { ContractRefusal, message } from "../errors.js";

import { ServerOperationLimits, readChunk, writeLimit } from "../operation-limits.js";

import { getServerStatus } from "../application/diagnostics.js";
import { NodeOpcuaDiagnosticsPort } from "../adapters/opcua-diagnostics.js";

import { describeSecurity, securityConfig } from "../security.js";
import { SubscribeOptions, SubscriptionFilter } from "../subscriptions.js";
import { ToolPolicy, serverIdentityRecord } from "../policy.js";

import type { FeatureHandlers } from "./dispatch.js";
export class ProtocolFeatureHandlers implements FeatureHandlers {
  constructor(
    private readonly runtime: NodeOpcuaRuntime,
    private readonly policy: ToolPolicy,
    private readonly operationLimits: () => Promise<ServerOperationLimits>,
    private readonly requireSession: () => ReturnType<NodeOpcuaRuntime["requireSession"]>
  ) {}
  private get conn() {
    return this.runtime.conn;
  }
  private get capabilities() {
    return this.runtime.capabilities;
  }
  private get metadata() {
    return this.runtime.metadata;
  }
  private get subs() {
    return this.runtime.subs;
  }
  private get events() {
    return this.runtime.events;
  }
  async getServerStatus(): Promise<ServerStatusReport> {
    const port = new NodeOpcuaDiagnosticsPort(
      this.conn,
      () => this.requireSession(),
      () => capabilityStatus(this.capabilities)
    );
    return await getServerStatus(
      port,
      describeSecurity(securityConfig()),
      serverIdentityRecord(this.policy.config)
    );
  }

  async readOpcuaNodes(nodeIds: string[]) {
    const port = new NodeOpcuaReadPort(this.requireSession(), this.metadata);
    return recordBlocks(await readNodes(port, nodeIds, readChunk(await this.operationLimits())));
  }

  async readOpcuaHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    aggregateFunction?: string;
    processingInterval: number;
  }) {
    const port = new NodeOpcuaHistoryPort(this.requireSession());
    const result = await readHistory(port, request, this.capabilities.aggregateFunctions);
    return historyResult(result.records, result.completeness, "historyTruncated");
  }

  async browseOpcuaNodes(request: BrowseRequest) {
    const port = new NodeOpcuaBrowsePort(this.requireSession(), () => this.operationLimits());
    const result = await browseNodes(port, request);
    return objectResult(result.result, result.completeness);
  }

  async writeOpcuaNodes(nodes: WriteRequest[]) {
    const session = this.requireSession();
    if (!Array.isArray(nodes) || nodes.length === 0) {
      throw new ContractRefusal(
        message("emptyArray", { tool: "write_opcua_nodes", argument: "nodes" })
      );
    }
    const port = new NodeOpcuaWritePort(session, this.metadata);
    const serverLimits = await this.operationLimits();
    const bounds = new Map(
      nodes.map((node, index) => [
        index,
        this.policy.boundFor(String(node?.node_id ?? ""))?.maxChange ?? null,
      ])
    );
    return recordBlocks(
      await writeNodes(
        port,
        nodes,
        { write: writeLimit(serverLimits), read: readChunk(serverLimits) },
        bounds,
        this.policy.config.allowOutOfRangeWrites
      )
    );
  }

  async callOpcuaMethod(objectNodeId: string, methodNodeId: string, methodArgs?: unknown[]) {
    const port = new NodeOpcuaMethodPort(this.requireSession());
    return objectResult(await callMethod(port, objectNodeId, methodNodeId, methodArgs));
  }

  subscriptionPort(): import("../application/subscriptions.js").SubscriptionPort {
    return new NodeOpcuaSubscriptionPort(() => this.requireSession(), this.subs, this.metadata);
  }

  async subscribeOpcuaNodes(
    nodeIds: string[],
    options: SubscribeOptions,
    filter: SubscriptionFilter
  ) {
    const result = await subscribeNodes(this.subscriptionPort(), nodeIds, options, filter);
    return recordBlocks(result.records, result.completeness);
  }

  async unsubscribeOpcuaNodes(subscriptionIds: string[]) {
    const result = await unsubscribeNodes(this.subscriptionPort(), subscriptionIds);
    return recordBlocks(result.records, result.completeness);
  }

  async subscribeEvents(nodeId: string, severityMin: number, requested: number) {
    return objectResult(await subscribeEvents(this.eventPort(), nodeId, severityMin, requested));
  }

  async readEvents(nodeId: string, limit: number) {
    const result = await readEvents(this.eventPort(), nodeId, limit);
    let response = eventResult(result.records, result.completeness);
    for (const text of result.notices) response = withNotice(response, text);
    return response;
  }

  async readEventHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    severityMin: number;
  }) {
    const result = await readEventHistory(this.eventPort(), request);
    return historyResult(result.records, result.completeness, "eventHistoryTruncated");
  }

  alarmPort() {
    return new NodeOpcuaAlarmPort(() => this.requireSession(), this.events);
  }

  async listActiveAlarms(nodeId: string, timeoutSeconds: number) {
    return eventResult(await listAlarms(this.alarmPort(), nodeId, timeoutSeconds));
  }

  async actOnAlarm(
    eventId: string,
    action: string,
    comment: string,
    durationMs: number | null,
    conditionId: string | undefined,
    acknowledgement: boolean
  ) {
    return objectResult(
      await actOnAlarm(
        this.alarmPort(),
        eventId,
        action,
        comment,
        durationMs,
        conditionId,
        acknowledgement
      )
    );
  }
  private eventPort() {
    return new NodeOpcuaEventPort(this.requireSession(), this.events);
  }
}
