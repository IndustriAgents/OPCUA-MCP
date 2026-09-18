// OPC UA data-change subscriptions and their monitored items.
//
// An MCP tool call is request/response, so a subscription cannot answer the
// caller directly: the notifications arrive whenever the OPC UA server decides
// to publish, long after `subscribe_opcua_nodes` has returned. What this manager
// does instead is own the OPC UA subscription and *buffer* what it delivers, so
// the agent can read the accumulated changes back at its own pace — through
// `list_subscriptions` or the `opcua://subscriptions` resource.
//
// `subscriptions.py` in the Python server is the other implementation of the
// same contract (`resultShapes.subscriptionRecords`), and the two must produce
// the same record for the same subscription.
import {
  AttributeIds,
  ClientMonitoredItem,
  ClientSession,
  ClientSubscription,
  DataValue,
  StatusCodes,
  TimestampsToReturn,
} from "node-opcua-client";

import { CONTRACT } from "./contract.js";
import { HistoryRecord, toHistoryRecord } from "./records.js";

// Defaults and bounds, read from the contract rather than written here. They
// were five constants declared identically in this file and in
// `subscriptions.py` — two copies of the same promise, which the tool
// descriptions also quote, so a change had to be made in three places to be
// true. Now it is made in one.
const LIMITS = CONTRACT.subscriptions;
export const DEFAULT_PUBLISHING_INTERVAL = LIMITS.defaultPublishingIntervalMs;
export const MIN_PUBLISHING_INTERVAL = LIMITS.minPublishingIntervalMs;
export const DEFAULT_BUFFER_SIZE = LIMITS.defaultBufferSize;
export const MIN_BUFFER_SIZE = LIMITS.minBufferSize;
export const MAX_BUFFER_SIZE = LIMITS.maxBufferSize;

/** One subscription as the contract describes it (`subscriptionRecords`). */
export interface SubscriptionRecord {
  subscription_id: string;
  node_id: string;
  publishing_interval: number;
  sampling_interval: number;
  buffer_size: number;
  change_count: number;
  changes: HistoryRecord[];
}

export interface SubscribeOptions {
  publishingInterval?: number;
  samplingInterval?: number;
  bufferSize?: number;
}

interface Entry {
  id: string;
  nodeId: string;
  publishingInterval: number;
  samplingInterval: number;
  bufferSize: number;
  changeCount: number;
  changes: HistoryRecord[];
  subscription: ClientSubscription | null;
  monitoredItem: ClientMonitoredItem | null;
}

/** A finite number, or the fallback when the caller sent nothing usable. */
function orDefault(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/** The intervals and buffer size a request resolves to, before any OPC UA call.
 *
 * Exported, and free of any OPC UA type, so the unit suites on both sides can
 * pin the same defaults: these values appear verbatim in the record the agent
 * reads back, so a difference between the runtimes is visible drift.
 */
export function resolveOptions(options: SubscribeOptions): {
  publishingInterval: number;
  samplingInterval: number;
  bufferSize: number;
} {
  const publishingInterval = Math.max(
    orDefault(options.publishingInterval, DEFAULT_PUBLISHING_INTERVAL),
    MIN_PUBLISHING_INTERVAL
  );
  // 0 means "sample as often as you publish" — resolved here rather than passed
  // through, so the record reports the interval that is actually in force.
  const requestedSampling = orDefault(options.samplingInterval, 0);
  const samplingInterval = requestedSampling > 0 ? requestedSampling : publishingInterval;
  const bufferSize = clamp(
    Math.trunc(orDefault(options.bufferSize, DEFAULT_BUFFER_SIZE)),
    MIN_BUFFER_SIZE,
    MAX_BUFFER_SIZE
  );
  return { publishingInterval, samplingInterval, bufferSize };
}

/** The message both runtimes give for an ID that is not (or no longer) active. */
export function unknownSubscriptionMessage(id: string): string {
  return `No such subscription: ${id}`;
}

/** The same, for a batch cancel — named in full so the caller can see which failed.
 *
 * Every id is checked before any subscription is cancelled, so this message
 * means nothing was cancelled: a partial cancel would leave the caller unable to
 * tell which handles still work, and would lose the buffered changes of the ones
 * that did go, to a typo.
 */
export function unknownSubscriptionsMessage(ids: string[]): string {
  return ids.length === 1
    ? unknownSubscriptionMessage(ids[0])
    : `No such subscriptions: ${ids.join(", ")}. Nothing was cancelled.`;
}

/** The message both runtimes give when the OPC UA server refuses an explicit cancel.
 *
 * Worth saying out loud rather than swallowing: the subscription is gone from
 * this process either way, so the ID cannot be retried, but the OPC UA server
 * may still be publishing into the void. Only the explicit path reports this —
 * on shutdown a refused delete is the normal case, not news.
 */
export function terminateFailedMessage(id: string, reason: string): string {
  return (
    `Cancelled ${id} here, but the OPC UA server did not accept the delete: ${reason}. ` +
    `It may keep publishing until the subscription's lifetime expires.`
  );
}

export class SubscriptionManager {
  private entries = new Map<string, Entry>();
  private counter = 0;

  /** Start monitoring `nodeId`, returning the record for the new subscription. */
  async subscribe(
    session: ClientSession,
    nodeId: string,
    options: SubscribeOptions = {}
  ): Promise<SubscriptionRecord> {
    const { publishingInterval, samplingInterval, bufferSize } = resolveOptions(options);

    // Claim the ID before the first OPC UA call, as the Python server does, so a
    // failed subscribe consumes one on both runtimes. IDs are opaque handles and
    // a gap means nothing — but an agent watching both should not see them
    // number differently.
    const id = `sub-${++this.counter}`;

    const entry: Entry = {
      id,
      nodeId,
      publishingInterval,
      samplingInterval,
      bufferSize,
      changeCount: 0,
      changes: [],
      subscription: null,
      monitoredItem: null,
    };

    await this.attach(session, entry);
    this.entries.set(entry.id, entry);
    return toRecord(entry);
  }

  /** Re-create every subscription on `session`, after the old one died.
   *
   * What makes an OPC UA MCP server survivable across a plant restart: the
   * subscription IDs the agent is holding keep working, and the changes already
   * buffered are still there to be read — only the gap while the server was away
   * is missing, which no amount of client-side effort could have filled.
   *
   * Best-effort per subscription. One the server will not take back (its node is
   * gone from the new address space, say) is dropped rather than left in the list
   * as a handle that will never deliver again: `list_subscriptions` has to keep
   * telling the truth.
   */
  async reattach(session: ClientSession): Promise<void> {
    for (const entry of [...this.entries.values()]) {
      try {
        await this.attach(session, entry);
        console.error(`Re-established subscription ${entry.id} on node ${entry.nodeId}`);
      } catch (error) {
        this.entries.delete(entry.id);
        console.error(
          `Could not re-establish subscription ${entry.id} on node ${entry.nodeId}: ${
            error instanceof Error ? error.message : String(error)
          }`
        );
      }
    }
  }

  /** Create the OPC UA subscription and monitored item behind one entry.
   *
   * Shared by the first subscribe and by every re-establishment after a
   * reconnect, so the two cannot drift into asking the server for different
   * things — the record the agent reads back names intervals that would
   * otherwise silently stop being the ones in force.
   */
  private async attach(session: ClientSession, entry: Entry): Promise<void> {
    const { nodeId, publishingInterval, samplingInterval, bufferSize } = entry;

    const subscription = await session.createSubscription2({
      requestedPublishingInterval: publishingInterval,
      // Keep-alives and lifetime are expressed in publishing intervals, so
      // deriving them from a count rather than a duration keeps a fast
      // subscription from expiring between two quiet publishes.
      requestedMaxKeepAliveCount: 10,
      requestedLifetimeCount: 60,
      maxNotificationsPerPublish: 100,
      publishingEnabled: true,
      priority: 10,
    });

    try {
      const monitoredItem = await subscription.monitor(
        { nodeId, attributeId: AttributeIds.Value },
        { samplingInterval, discardOldest: true, queueSize: bufferSize },
        TimestampsToReturn.Both
      );
      // node-opcua reports a rejected item through the create result rather than
      // by throwing, so an unreadable node would otherwise leave a subscription
      // that silently never fires.
      if (monitoredItem.statusCode && monitoredItem.statusCode !== StatusCodes.Good) {
        throw new Error(`Monitoring rejected with status: ${monitoredItem.statusCode.toString()}`);
      }
      monitoredItem.on("changed", (dataValue: DataValue) => this.record(entry, dataValue));
      entry.subscription = subscription;
      entry.monitoredItem = monitoredItem;
    } catch (error) {
      // Never leave the OPC UA server holding a subscription this process has
      // forgotten about: it would keep publishing until its lifetime expires.
      await terminateQuietly(subscription);
      throw new Error(
        `Failed to subscribe to node ${nodeId}: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
    }
  }

  /** Every active subscription, in the order it was created. */
  list(): SubscriptionRecord[] {
    return [...this.entries.values()].map(toRecord);
  }

  /** Cancel one subscription, returning its final record. Throws if unknown. */
  async unsubscribe(id: string): Promise<SubscriptionRecord> {
    const entry = this.entries.get(id);
    if (!entry) {
      throw new Error(unknownSubscriptionMessage(id));
    }
    // Drop it from the map first: even if terminate() fails, the agent must not
    // be told a subscription is still active when nothing is listening to it.
    this.entries.delete(id);
    const record = toRecord(entry);
    if (!entry.subscription) return record;
    try {
      await entry.subscription.terminate();
    } catch (error) {
      // Unlike shutdown, an explicit cancel reports this. The caller asked for
      // something specific and did not fully get it, and no longer holds an ID
      // to retry with.
      throw new Error(
        terminateFailedMessage(id, error instanceof Error ? error.message : String(error))
      );
    }
    return record;
  }

  /** Tear every subscription down — the shutdown path. */
  async closeAll(): Promise<void> {
    const entries = [...this.entries.values()];
    this.entries.clear();
    for (const entry of entries) {
      if (entry.subscription) await terminateQuietly(entry.subscription);
    }
  }

  private record(entry: Entry, dataValue: DataValue): void {
    entry.changeCount += 1;
    entry.changes.push(toHistoryRecord(dataValue));
    if (entry.changes.length > entry.bufferSize) {
      entry.changes.splice(0, entry.changes.length - entry.bufferSize);
    }
  }
}

function toRecord(entry: Entry): SubscriptionRecord {
  return {
    subscription_id: entry.id,
    node_id: entry.nodeId,
    publishing_interval: entry.publishingInterval,
    sampling_interval: entry.samplingInterval,
    buffer_size: entry.bufferSize,
    change_count: entry.changeCount,
    changes: [...entry.changes],
  };
}

/** Terminate without letting a dead session's error escape.
 *
 * The remaining callers are cleanup paths: an OPC UA server that has already
 * dropped the subscription (or the whole session) is the normal case on
 * shutdown, and failing there would turn a tidy exit into a crash. An explicit
 * `unsubscribe` does *not* go through here — see `terminateFailedMessage`.
 */
async function terminateQuietly(subscription: ClientSubscription): Promise<void> {
  try {
    await subscription.terminate();
  } catch (error) {
    console.error("Error terminating OPC UA subscription:", error);
  }
}
