/** Subscription lifecycle decisions over an instance-owned, session-lazy port. */
import { CONTRACT } from "../contract.js";
import { AdapterFailure, ContractRefusal, describeError, message } from "../errors.js";
import { bufferCompleteness } from "../completeness.js";
import { MAX_SUBSCRIPTIONS } from "../limits.js";
export interface SubscriptionRecord {
  subscription_id: string;
  dropped: number;
}
export interface SubscribeOptions {
  publishingInterval?: number;
  samplingInterval?: number;
  bufferSize?: number;
}
export interface SubscriptionFilter {
  deadbandType: string;
  deadbandValue: number;
  trigger: string;
}
const DEADBAND_TYPES = { none: true, absolute: true, percent: true };
const DATA_CHANGE_TRIGGERS = { status: true, statusValue: true, statusValueTimestamp: true };
const DEFAULT_DATA_CHANGE_TRIGGER = CONTRACT.subscriptions.defaultDataChangeTrigger;
function orDefault(value: number | undefined | null, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
export function resolveFilter(options: {
  deadbandType?: string | null;
  deadbandValue?: number | null;
  dataChangeTrigger?: string | null;
}): SubscriptionFilter {
  const deadbandType = options.deadbandType ?? "none";
  if (!Object.hasOwn(DEADBAND_TYPES, deadbandType)) {
    throw new ContractRefusal(
      message("notAllowedValue", {
        tool: "subscribe_opcua_nodes",
        argument: "deadband_type",
        allowed: Object.keys(DEADBAND_TYPES)
          .map((name) => JSON.stringify(name))
          .join(", "),
        value: JSON.stringify(deadbandType),
      })
    );
  }
  const trigger = options.dataChangeTrigger ?? DEFAULT_DATA_CHANGE_TRIGGER;
  if (!Object.hasOwn(DATA_CHANGE_TRIGGERS, trigger)) {
    throw new ContractRefusal(
      message("notAllowedValue", {
        tool: "subscribe_opcua_nodes",
        argument: "data_change_trigger",
        allowed: Object.keys(DATA_CHANGE_TRIGGERS)
          .map((name) => JSON.stringify(name))
          .join(", "),
        value: JSON.stringify(trigger),
      })
    );
  }
  if (deadbandType === "none") {
    return { deadbandType: "none", deadbandValue: 0, trigger };
  }
  if (options.deadbandValue === undefined || options.deadbandValue === null) {
    throw new ContractRefusal(message("deadbandNeedsValue", { deadband_type: deadbandType }));
  }
  return {
    deadbandType,
    deadbandValue: orDefault(options.deadbandValue, 0),
    trigger,
  };
}

export interface SubscriptionPort {
  list(): SubscriptionRecord[];
  ranges(nodeIds: string[]): Promise<Map<string, boolean>>;
  subscribe(
    nodeId: string,
    options: SubscribeOptions,
    filter: SubscriptionFilter
  ): Promise<SubscriptionRecord>;
  unsubscribe(id: string): Promise<SubscriptionRecord>;
}
export function listSubscriptions(port: SubscriptionPort) {
  const records = port.list();
  return { records, completeness: bufferCompleteness(records) };
}
export async function subscribeNodes(
  port: SubscriptionPort,
  nodeIds: string[],
  options: SubscribeOptions,
  filter: SubscriptionFilter
) {
  if (!Array.isArray(nodeIds) || nodeIds.length === 0)
    throw new ContractRefusal(
      message("emptyArray", { tool: "subscribe_opcua_nodes", argument: "node_ids" })
    );
  const active = port.list().length;
  if (active + nodeIds.length > MAX_SUBSCRIPTIONS)
    throw new ContractRefusal(
      message("tooManySubscriptions", { active, limit: MAX_SUBSCRIPTIONS, wanted: nodeIds.length })
    );
  if (filter.deadbandType === "percent") {
    const ranges = await port.ranges(nodeIds);
    for (const nodeId of nodeIds)
      if (!ranges.get(nodeId))
        throw new ContractRefusal(message("percentDeadbandNeedsRange", { node_id: nodeId }));
  }
  const records: SubscriptionRecord[] = [];
  for (const nodeId of nodeIds) {
    try {
      records.push(await port.subscribe(nodeId, options, filter));
    } catch (error) {
      throw new AdapterFailure(
        "subscribe",
        message("subscribeFailed", { node_id: nodeId, reason: describeError(error) }),
        error
      );
    }
  }
  return { records, completeness: bufferCompleteness(records) };
}
export async function unsubscribeNodes(port: SubscriptionPort, ids: string[]) {
  if (!Array.isArray(ids) || ids.length === 0)
    throw new ContractRefusal(
      message("emptyArray", { tool: "unsubscribe_opcua_nodes", argument: "subscription_ids" })
    );
  const active = new Set(port.list().map((record) => record.subscription_id));
  const unknown = ids.filter((id) => !active.has(id));
  if (unknown.length)
    throw new ContractRefusal(
      unknown.length === 1
        ? message("unknownSubscription", { subscription_id: unknown[0] })
        : message("unknownSubscriptions", { subscription_ids: unknown.join(", ") })
    );
  const records: SubscriptionRecord[] = [];
  for (const id of ids) records.push(await port.unsubscribe(id));
  return { records, completeness: bufferCompleteness(records) };
}
