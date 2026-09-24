// OPC UA events and Alarms & Conditions: the filter, the record shape, and the
// per-notifier event buffers the `subscribe_events` / `read_events` pair uses.
//
// `contract/tools.json` -> `events` is the specification; this module is the Node
// implementation of it and `events.py` in the Python server is the other. Both
// build their EventFilter select clauses from the same field list, in the same
// order, and name the resulting record fields the same way — so a client that
// has learned one server's events can read the other's.
//
// Why a buffer at all: MCP is request/response, and an OPC UA event arrives when
// the server decides. `subscribe_events` therefore starts a real subscription and
// parks what arrives; `read_events` drains it. Nothing is pushed to the client.
import {
  AttributeIds,
  ClientMonitoredItem,
  ClientSession,
  ClientSubscription,
  DataType,
  LocalizedText,
  StatusCodes,
  TimestampsToReturn,
  Variant,
  coerceNodeId,
  constructEventFilter,
  HistoryReadRequest,
  HistoryReadValueId,
  ReadEventDetails,
  resolveNodeId,
} from "node-opcua-client";

import { CONTRACT } from "./contract.js";
import { message } from "./errors.js";
import { continues, releaseContinuationPoint } from "./history.js";
import { variantToJson } from "./records.js";
import { notice } from "./notices.js";

const EVENTS = CONTRACT.events;

/** The default notifier: the Server object, where most servers raise everything. */
export const DEFAULT_NOTIFIER = EVENTS.defaultNotifierNodeId;

/** The defaults the contract's tool descriptions promise, shared with Python. */
export const EVENT_DEFAULTS = EVENTS.defaults;

/** One event as the canonical record (contract -> resultShapes.eventRecords). */
export type EventRecord = Record<string, unknown>;

/** The message both runtimes give when a ConditionRefresh does not finish.
 *
 * Said out loud rather than swallowed. The server answers a refresh with a
 * RefreshEnd event, and without one there is no way to know whether the
 * conditions collected so far are all of them — returning them as if they were
 * would let `list_active_alarms` quietly under-report retained alarms, which in
 * an industrial setting is the one failure this tool must not have.
 */
export function refreshTimedOutMessage(timeoutSeconds: number, collected: number): string {
  return (
    `ConditionRefresh did not finish within ${timeoutSeconds}s: the server sent ` +
    `${collected} condition(s) but no RefreshEnd, so there may be more. ` +
    `Retry with a larger timeout_seconds.`
  );
}

/** The message both runtimes give when the event buffer overflowed before a read.
 *
 * Reported to the caller, not only to stderr: an agent that cannot tell a
 * complete event stream from one that lost alarms during a burst will read the
 * gap as quiet.
 */
export function droppedEventsMessage(dropped: number, bufferSize: number): string {
  return notice("droppedEvents", { dropped, buffer_size: bufferSize });
}

/** The subscription parameters both the buffered and the ConditionRefresh paths use.
 *
 * `queueSize` is the load-bearing one. A ConditionRefresh answers with the
 * RefreshStart event, every retained condition, and the RefreshEnd event in one
 * publishing cycle; with the default queue of 1 the server discards all but the
 * last and the refresh looks like it found no alarms at all.
 */
const PUBLISHING_INTERVAL_MS = 200;
const QUEUE_SIZE = 1000;

function subscriptionRequest() {
  return {
    requestedPublishingInterval: PUBLISHING_INTERVAL_MS,
    requestedLifetimeCount: 1000,
    requestedMaxKeepAliveCount: 20,
    maxNotificationsPerPublish: 10000,
    publishingEnabled: true,
    priority: 1,
  };
}

/** The contract's select clauses: what both servers ask a server to report.
 *
 * `constructEventFilter` gives "ConditionId" the empty-browse-path treatment the
 * spec asks for; every other path is resolved against BaseEventType. See the
 * contract's `events` comment, and `_select_clause` in the Python server.
 */
export function eventSelectClauses() {
  return buildEventFilter().selectClauses ?? [];
}

function buildEventFilter() {
  return constructEventFilter(EVENTS.fields.map((field) => field.path));
}

/** Monitor `nodeId`'s events with the contract's select clauses. */
async function monitorEvents(
  subscription: ClientSubscription,
  nodeId: string
): Promise<ClientMonitoredItem> {
  return await subscription.monitor(
    { nodeId, attributeId: AttributeIds.EventNotifier },
    {
      samplingInterval: 0,
      discardOldest: true,
      queueSize: QUEUE_SIZE,
      filter: buildEventFilter(),
    },
    TimestampsToReturn.Both
  );
}

/** The select-clause values of one event, as the canonical record. */
export function toEventRecord(fields: Variant[]): EventRecord {
  const record: EventRecord = {};
  EVENTS.fields.forEach((field, index) => {
    record[field.key] = variantToJson(fields[index]);
  });
  return record;
}

/** The `event_type` of a record as a plain string, for comparing to a NodeId. */
function eventTypeOf(record: EventRecord): string {
  return typeof record.event_type === "string" ? record.event_type : "";
}

/** True for a record the server sent as part of answering a ConditionRefresh. */
function isRefreshMarker(record: EventRecord): boolean {
  const type = eventTypeOf(record);
  return type === EVENTS.refreshStartEventTypeNodeId || type === EVENTS.refreshEndEventTypeNodeId;
}

/** One notifier node's live subscription and the events it has collected. */
interface EventBuffer {
  session: ClientSession;
  subscription: ClientSubscription;
  severityMin: number;
  size: number;
  records: EventRecord[];
  /** Events the buffer dropped because it was full, reported once on read. */
  dropped: number;
}

/**
 * The event subscriptions this server holds, keyed by notifier node.
 *
 * Also remembers which condition each event_id came from, so `acknowledge_alarm`
 * can take the event_id the model just saw and nothing else: OPC UA needs both
 * the event_id and the condition's NodeId, but only one of them is worth asking
 * a model to carry around.
 */
export class EventSubscriptions {
  private buffers = new Map<string, EventBuffer>();
  private conditionOfEvent = new Map<string, string>();

  /** Remember the condition behind every event we hand out. */
  remember(records: EventRecord[]): void {
    for (const record of records) {
      const eventId = record.event_id;
      const conditionId = record.condition_id;
      if (typeof eventId === "string" && typeof conditionId === "string") {
        this.conditionOfEvent.set(eventId, conditionId);
      }
    }
  }

  /** The condition an event_id was reported against, if this server has seen it. */
  conditionFor(eventId: string): string | undefined {
    return this.conditionOfEvent.get(eventId);
  }

  /** Start (or restart) buffering events from `nodeId`. */
  async subscribe(
    session: ClientSession,
    nodeId: string,
    severityMin: number,
    bufferSize: number
  ): Promise<{ replaced: boolean }> {
    // Reported back to the caller: re-subscribing silently discards whatever the
    // previous subscription had buffered, and an agent that cannot tell that
    // happened reads the missing events as quiet.
    const replaced = await this.drop(nodeId);

    const subscription = await session.createSubscription2(subscriptionRequest());
    const buffer: EventBuffer = {
      session,
      subscription,
      severityMin,
      size: bufferSize,
      records: [],
      dropped: 0,
    };

    // Never leave the OPC UA server holding a subscription this process has
    // forgotten about: it would keep publishing until its lifetime expires, and
    // a handful of failed `subscribe_events` calls would eat the server's
    // subscription quota. Same guard as `subscriptions.ts` uses.
    let monitoredItem: ClientMonitoredItem;
    try {
      monitoredItem = await monitorEvents(subscription, nodeId);
    } catch (error) {
      await terminateQuietly(subscription);
      throw error;
    }

    monitoredItem.on("changed", (fields: Variant[]) => {
      const record = toEventRecord(fields);
      // The refresh markers are protocol bookkeeping, not plant events: a
      // ConditionRefresh triggered by `list_active_alarms` would otherwise
      // pepper every open buffer with them.
      if (isRefreshMarker(record)) return;
      if (typeof record.severity === "number" && record.severity < buffer.severityMin) return;
      buffer.records.push(record);
      if (buffer.records.length > buffer.size) {
        buffer.records.splice(0, buffer.records.length - buffer.size);
        buffer.dropped += 1;
      }
    });

    this.buffers.set(nodeId, buffer);
    return { replaced };
  }

  /** Take up to `limit` of the oldest buffered events, removing them. */
  drain(
    session: ClientSession,
    nodeId: string,
    limit: number
  ): { records: EventRecord[]; remaining: number; dropped: number; size: number } | null {
    const buffer = this.live(session, nodeId);
    if (!buffer) return null;

    const records = buffer.records.splice(0, limit);
    const dropped = buffer.dropped;
    buffer.dropped = 0;
    this.remember(records);
    return { records, remaining: buffer.records.length, dropped, size: buffer.size };
  }

  /** Tear down the subscription for `nodeId`, if there is one. */
  /** Tear down the subscription for `nodeId`. True when there was one. */
  async drop(nodeId: string): Promise<boolean> {
    const buffer = this.buffers.get(nodeId);
    if (!buffer) return false;
    this.buffers.delete(nodeId);
    await terminateQuietly(buffer.subscription);
    return true;
  }

  /** Tear every event subscription down — the shutdown path.
   *
   * Same rule as the data-change subscriptions in `subscriptions.ts`: an OPC UA
   * server left holding a subscription this process has forgotten keeps
   * publishing into the void until its lifetime expires, so they go before the
   * session does.
   */
  async closeAll(): Promise<void> {
    for (const nodeId of [...this.buffers.keys()]) {
      await this.drop(nodeId);
    }
  }

  /** The buffer for `nodeId`, provided it belongs to the current session.
   *
   * A subscription lives on the session that created it. After a reconnect the
   * old one is gone from the server, so treating a stale buffer as live would
   * hand back events that stopped arriving at the moment of the outage.
   */
  private live(session: ClientSession, nodeId: string): EventBuffer | undefined {
    const buffer = this.buffers.get(nodeId);
    if (!buffer) return undefined;
    if (buffer.session !== session) {
      this.buffers.delete(nodeId);
      return undefined;
    }
    return buffer;
  }
}

/**
 * The conditions the server is retaining right now, via ConditionRefresh.
 *
 * The server answers a refresh by re-sending every retained condition to one
 * subscription, bracketed by a RefreshStart and a RefreshEnd event. So this
 * creates a subscription of its own, asks, collects until the RefreshEnd (or the
 * timeout), and tears it down again — deliberately independent of whatever
 * `subscribe_events` may or may not have running.
 */
/** Events the server stored, for a range that has already passed (#117).
 *
 * `subscribe_events` only sees what arrives after it subscribes, so nothing
 * could answer "what fired in the ten minutes before the line stopped" — by the
 * time anyone asks, the events are gone. OPC UA Part 11 §6.5.2 defines
 * `ReadEventDetails` for exactly that, and a server that historises its events
 * already holds the answer.
 *
 * The select clauses are `buildEventFilter()`, the same ones the live
 * subscription sends, and the decoding is `toEventRecord`, the same decoder. A
 * historical alarm has to come back as the *same record* as a live one, or the
 * two are not comparable and an agent has to learn two shapes.
 *
 * Severity is filtered here rather than in a where clause, as the live path
 * does, so a server that mishandles a ContentFilter cannot silently drop events
 * on us.
 *
 * `performMessageTransaction` rather than a convenience wrapper because
 * node-opcua offers none for event history that lets the filter be supplied —
 * `readHistoryValue` is for values, and the ergonomic event helpers construct
 * their own select clauses, which is exactly what must not happen here.
 *
 * Returns what `completeness` needs as well as the records (issue #137): how many
 * events the server sent before the severity filter — a cap is reached by those,
 * not by the survivors — whether it returned a continuation point, and the time
 * of the last one, which is where a forward read resumes. The continuation point
 * itself is released rather than kept; see history.ts.
 */
export async function readEventHistory(
  session: ClientSession,
  nodeId: string,
  startTime: Date,
  endTime: Date,
  numValues: number,
  severityMin: number
): Promise<EventHistoryPage> {
  const details = new ReadEventDetails({
    numValuesPerNode: numValues,
    startTime,
    endTime,
    filter: buildEventFilter(),
  });
  const request = new HistoryReadRequest({
    historyReadDetails: details,
    timestampsToReturn: TimestampsToReturn.Both,
    releaseContinuationPoints: false,
    nodesToRead: [new HistoryReadValueId({ nodeId: resolveNodeId(nodeId) })],
  });

  const response: any = await new Promise((resolve, reject) =>
    (session as any).performMessageTransaction(request, (error: Error | null, result: unknown) =>
      error ? reject(error) : resolve(result)
    )
  );

  const result = response.results?.[0];
  if (!result) throw new Error("Read event history failed");
  if (result.statusCode !== StatusCodes.Good) {
    throw new Error(`Read event history failed with status: ${result.statusCode.name}`);
  }

  const continued = continues(result.continuationPoint);
  await releaseContinuationPoint(session, nodeId, result.continuationPoint, details);

  const fetched: EventRecord[] = (result.historyData?.events ?? []).map(
    (event: { eventFields: Variant[] }) => toEventRecord(event.eventFields)
  );
  const last = fetched.at(-1)?.time;
  return {
    records: fetched.filter((record) => severityAtLeast(record, severityMin)),
    fetched: fetched.length,
    continued,
    lastTime: typeof last === "string" ? last : null,
  };
}

/** One event-history read, with what `completeness` is built from. */
export interface EventHistoryPage {
  records: EventRecord[];
  /** Events the server sent, before `severity_min` filtered any out. */
  fetched: number;
  /** Whether the server said it holds more. */
  continued: boolean;
  /** When the last event the server sent occurred, or null. */
  lastTime: string | null;
}

/** Whether one record clears the severity floor.
 *
 * An event carrying no severity is kept unless a floor was actually asked for:
 * dropping it on a default call would lose events for saying nothing, and
 * keeping it on an explicit `severity_min` would answer a question about
 * urgency with an event that has no urgency. 0 is "no floor", as it is for
 * `subscribe_events`.
 */
function severityAtLeast(record: EventRecord, severityMin: number): boolean {
  const severity = record.severity;
  if (typeof severity !== "number") return severityMin <= 0;
  return severity >= severityMin;
}

export async function listActiveAlarms(
  session: ClientSession,
  nodeId: string,
  timeoutSeconds: number
): Promise<EventRecord[]> {
  const subscription = await session.createSubscription2(subscriptionRequest());
  try {
    const monitoredItem = await monitorEvents(subscription, nodeId);

    const conditions: EventRecord[] = [];
    let started = false;
    let ended = false;

    const finished = new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, Math.max(0, timeoutSeconds) * 1000);
      monitoredItem.on("changed", (fields: Variant[]) => {
        const record = toEventRecord(fields);
        const type = eventTypeOf(record);
        if (type === EVENTS.refreshStartEventTypeNodeId) {
          // Anything before this belongs to the live event stream, not to the
          // refresh, and would be reported as an alarm it is not.
          started = true;
          return;
        }
        if (type === EVENTS.refreshEndEventTypeNodeId) {
          ended = true;
          clearTimeout(timer);
          resolve();
          return;
        }
        if (started && record.condition_id !== null) {
          conditions.push(record);
        }
      });
    });

    const statusCode = await callConditionRefresh(session, subscription.subscriptionId);
    if (statusCode !== StatusCodes.Good) {
      throw new Error(
        `ConditionRefresh failed with status: ${statusCode.toString()}. ` +
          "The server may not implement OPC UA Alarms & Conditions."
      );
    }

    await finished;
    if (!ended) {
      throw new Error(refreshTimedOutMessage(timeoutSeconds, conditions.length));
    }
    return conditions;
  } finally {
    await terminateQuietly(subscription);
  }
}

/** Terminate without letting a dead session's error escape.
 *
 * Every caller is a cleanup path: a subscription the OPC UA server has already
 * dropped (with the session, usually) is the normal case there, and failing
 * would turn a tidy teardown into a crash. `subscriptions.ts` has the same.
 */
async function terminateQuietly(subscription: ClientSubscription): Promise<void> {
  try {
    await subscription.terminate();
  } catch (error) {
    console.error("Error terminating OPC UA event subscription:", error);
  }
}

/** Ask the server to re-send its retained conditions to one subscription. */
async function callConditionRefresh(session: ClientSession, subscriptionId: number) {
  const callResult = await session.call({
    objectId: EVENTS.conditionTypeNodeId,
    methodId: EVENTS.conditionRefreshMethodNodeId,
    inputArguments: [new Variant({ dataType: DataType.UInt32, value: subscriptionId })],
  });
  return callResult.statusCode;
}

/** What each alarm action calls, and what it takes, from the contract.
 *
 * One table rather than a branch per action: `acknowledge_alarm` and
 * `act_on_alarm` are the same operation with a different method id, and the
 * surface carries them separately only because renaming the first would break
 * every caller.
 */
interface ActionSpec {
  browseName: string;
  methodNodeId: string;
  on: string;
  takes: string;
}

export const ACTIONS: Record<string, ActionSpec> = Object.fromEntries(
  Object.entries(EVENTS.actions).filter(([name]) => !name.startsWith("$"))
) as Record<string, ActionSpec>;

/** Call one Part 9 §5.5 method on one condition instance.
 *
 * *Which* object the method hangs off is the action's own business — see `on` in
 * the table above — and Part 9 allows a server not to expose condition instances
 * in its address space at all, in which case the type's own method is called with
 * the condition as the object. That well-known id is the fallback here, exactly
 * as `events.py` does it.
 */
export async function alarmAction(
  session: ClientSession,
  conditionId: string,
  eventId: string,
  action: string,
  comment = "",
  durationMs: number | null = null
) {
  const spec = ACTIONS[action];
  // Which object the method hangs off. The acknowledge family are methods of the
  // condition's own type; the shelving ones are methods of
  // ShelvedStateMachineType and hang off the condition's ShelvingState component
  // instead. Calling one on the wrong object does not fail cleanly — the server
  // resolves a *different* method of the right name's neighbour and answers
  // BadArgumentsMissing or BadTooManyArguments.
  const objectId =
    spec.on === "condition" ? conditionId : await shelvingStateId(session, conditionId);
  const methodId = await actionMethodId(session, objectId, spec);

  let inputArguments: Variant[] = [];
  if (spec.takes === "eventIdAndComment") {
    inputArguments = [
      new Variant({ dataType: DataType.ByteString, value: Buffer.from(eventId, "base64") }),
      new Variant({
        dataType: DataType.LocalizedText,
        value: new LocalizedText({ text: comment }),
      }),
    ];
  } else if (spec.takes === "duration") {
    // Duration is a Double of milliseconds in OPC UA, not a struct.
    inputArguments = [new Variant({ dataType: DataType.Double, value: durationMs ?? 0 })];
  }

  const result = await session.call({ objectId, methodId, inputArguments });
  return result.statusCode;
}

/** Acknowledge one condition instance. The first stage of Part 9's handshake.
 *
 * Kept as its own name because `acknowledge_alarm` is a tool callers already
 * have; the work is `alarmAction`'s, so there is only ever one implementation to
 * drift.
 */
export async function acknowledgeAlarm(
  session: ClientSession,
  conditionId: string,
  eventId: string,
  comment: string
) {
  return alarmAction(session, conditionId, eventId, "acknowledge", comment);
}

/** The condition's own method, or its type's well-known one when it has none.
 *
 * Part 9 allows a server not to expose condition instances in its address space
 * at all, in which case the type's method is called with the condition as the
 * object — which is what the fallback is for.
 */
async function actionMethodId(
  session: ClientSession,
  objectId: string,
  spec: { browseName: string; methodNodeId: string }
): Promise<string> {
  const found = await childByBrowseName(session, objectId, spec.browseName);
  return found ?? spec.methodNodeId;
}

/** The condition's ShelvingState, or throw saying this server has none.
 *
 * ShelvingState is optional in Part 9, and a server without it is still
 * conformant — so this is a refusal to word rather than a bug to hide. Falling
 * back to the type node would be worse than useless: shelving is per-instance
 * state, and a call against the type would either fail obscurely or change
 * something nobody asked about.
 */
async function shelvingStateId(session: ClientSession, conditionId: string): Promise<string> {
  const found = await childByBrowseName(session, conditionId, EVENTS.shelvingStateBrowseName);
  if (found) return found;
  throw new Error(message("shelvingNotSupported", { condition_id: conditionId }));
}

/** One forward reference of `parentId` by browse name, or null. */
async function childByBrowseName(
  session: ClientSession,
  parentId: string,
  browseName: string
): Promise<string | null> {
  try {
    const browseResult = await session.browse({
      nodeId: coerceNodeId(parentId),
      browseDirection: 0, // Forward
      resultMask: 63, // Everything, BrowseName included
    });
    for (const reference of browseResult.references ?? []) {
      if (reference.browseName.name === browseName) {
        return reference.nodeId.toString();
      }
    }
  } catch (error) {
    console.error(`Could not browse ${parentId} for ${browseName}:`, error);
  }
  return null;
}
