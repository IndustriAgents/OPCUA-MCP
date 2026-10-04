/** Contract dispatch over protocol handlers; no native session or SDK operations. */
import type { ToolName } from "../generated/contract-types.js";
import type { BrowseRequest } from "../application/browse.js";
import type { WriteRequest } from "../application/write.js";
import {
  listSubscriptions,
  resolveFilter,
  type SubscribeOptions,
  type SubscriptionFilter,
  type SubscriptionPort,
} from "../application/subscriptions.js";
import { ToolFailure, message } from "../errors.js";
import { CONTRACT } from "../contract.js";
import {
  recordBlocks,
  statusResult,
  type ProtocolResult,
  type ServerStatusReport,
} from "./results.js";
const DEFAULT_NOTIFIER = CONTRACT.events.defaultNotifierNodeId;
const EVENT_DEFAULTS = CONTRACT.events.defaults;
export interface FeatureHandlers {
  getServerStatus(): Promise<ServerStatusReport>;
  readOpcuaNodes(nodeIds: string[]): ProtocolResult | Promise<ProtocolResult>;
  browseOpcuaNodes(request: BrowseRequest): ProtocolResult | Promise<ProtocolResult>;
  readOpcuaHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    aggregateFunction?: string;
    processingInterval: number;
  }): ProtocolResult | Promise<ProtocolResult>;
  readEventHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    severityMin: number;
  }): ProtocolResult | Promise<ProtocolResult>;
  writeOpcuaNodes(nodes: WriteRequest[]): ProtocolResult | Promise<ProtocolResult>;
  callOpcuaMethod(
    objectNodeId: string,
    methodNodeId: string,
    methodArgs?: unknown[]
  ): ProtocolResult | Promise<ProtocolResult>;
  subscribeOpcuaNodes(
    nodeIds: string[],
    options: SubscribeOptions,
    filter: SubscriptionFilter
  ): ProtocolResult | Promise<ProtocolResult>;
  unsubscribeOpcuaNodes(subscriptionIds: string[]): ProtocolResult | Promise<ProtocolResult>;
  subscriptionPort(): SubscriptionPort;
  subscribeEvents(
    nodeId: string,
    severityMin: number,
    requested: number
  ): ProtocolResult | Promise<ProtocolResult>;
  readEvents(nodeId: string, limit: number): ProtocolResult | Promise<ProtocolResult>;
  listActiveAlarms(
    nodeId: string,
    timeoutSeconds: number
  ): ProtocolResult | Promise<ProtocolResult>;
  actOnAlarm(
    eventId: string,
    action: string,
    comment: string,
    durationMs: number | null,
    conditionId: string | undefined,
    acknowledgement: boolean
  ): ProtocolResult | Promise<ProtocolResult>;
}
export async function dispatchFeature(
  name: ToolName,
  args: Record<string, unknown>,
  handlers: FeatureHandlers
): Promise<ProtocolResult> {
  switch (name) {
    case "get_server_status":
      return statusResult(await handlers.getServerStatus());

    case "read_opcua_nodes":
      return await handlers.readOpcuaNodes(args.node_ids as string[]);

    case "browse_opcua_nodes":
      return await handlers.browseOpcuaNodes({
        nodeId: args.node_id as string | undefined,
        browsePath: args.browse_path as string | undefined,
        depth: args.depth as number | undefined,
        nodeClass: args.node_class as string | undefined,
        nameFilter: args.name_filter as string | undefined,
        includeValues: args.include_values as boolean | undefined,
        maxNodes: args.max_nodes as number | undefined,
      });

    case "read_opcua_history":
      return await handlers.readOpcuaHistory({
        nodeId: args.node_id as string,
        start: args.start_time as string | undefined,
        end: args.end_time as string | undefined,
        numValues: (args.num_values as number) || 0,
        aggregateFunction: args.aggregate_function as string | undefined,
        processingInterval: (args.processing_interval as number) || 0,
      });

    case "read_event_history":
      return await handlers.readEventHistory({
        nodeId: (args.node_id as string) || DEFAULT_NOTIFIER,
        start: args.start_time as string | undefined,
        end: args.end_time as string | undefined,
        numValues: (args.num_values as number) || 0,
        severityMin: (args.severity_min as number) || EVENT_DEFAULTS.severityMin,
      });

    case "write_opcua_nodes":
      return await handlers.writeOpcuaNodes(args.nodes as WriteRequest[]);

    case "call_opcua_method":
      return await handlers.callOpcuaMethod(
        args.object_node_id as string,
        args.method_node_id as string,
        args.arguments as unknown[] | undefined
      );

    case "subscribe_opcua_nodes":
      return await handlers.subscribeOpcuaNodes(
        args.node_ids as string[],
        {
          publishingInterval: args.publishing_interval as number | undefined,
          samplingInterval: args.sampling_interval as number | undefined,
          bufferSize: args.buffer_size as number | undefined,
        },
        resolveFilter({
          deadbandType: args.deadband_type as string | undefined,
          deadbandValue: args.deadband_value as number | undefined,
          dataChangeTrigger: args.data_change_trigger as string | undefined,
        })
      );

    case "unsubscribe_opcua_nodes":
      return await handlers.unsubscribeOpcuaNodes(args.subscription_ids as string[]);

    case "list_subscriptions":
      const listed = listSubscriptions(handlers.subscriptionPort());
      return recordBlocks(listed.records, listed.completeness);

    case "subscribe_events":
      return await handlers.subscribeEvents(
        (args.node_id as string) || DEFAULT_NOTIFIER,
        (args.severity_min as number) ?? EVENT_DEFAULTS.severityMin,
        (args.buffer_size as number) || EVENT_DEFAULTS.bufferSize
      );

    case "read_events":
      return handlers.readEvents(
        (args.node_id as string) || DEFAULT_NOTIFIER,
        (args.limit as number) || EVENT_DEFAULTS.readLimit
      );

    case "list_active_alarms":
      return await handlers.listActiveAlarms(
        (args.node_id as string) || DEFAULT_NOTIFIER,
        (args.timeout_seconds as number) ?? EVENT_DEFAULTS.refreshTimeoutSeconds
      );

    case "acknowledge_alarm":
      return await handlers.actOnAlarm(
        args.event_id as string,
        "acknowledge",
        (args.comment as string) ?? "",
        null,
        args.condition_id as string | undefined,
        true
      );

    case "act_on_alarm":
      return await handlers.actOnAlarm(
        args.event_id as string,
        args.action as string,
        (args.comment as string) ?? "",
        (args.shelve_duration_ms as number | undefined) ?? null,
        args.condition_id as string | undefined,
        false
      );

    default: {
      const unhandled: never = name;
      throw new ToolFailure(message("unknownTool", { tool: unhandled }));
    }
  }
}
