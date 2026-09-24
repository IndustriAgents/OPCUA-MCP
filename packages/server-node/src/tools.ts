// The OPC UA tool implementations.
//
// Adding a tool touches this file and contract/tools.json, and nothing else:
// `listTools` is generated from the contract, `callTool` dispatches by name, and
// the policy layer authorises it from the `guard` the contract declares.
import {
  AttributeIds,
  DataType,
  Variant,
  VariantArrayType,
  DataValue,
  CallMethodResult,
  NodeClass,
  AggregateFunction,
  BrowseDirection,
  ClientSession,
  ReadProcessedDetails,
  ReadRawModifiedDetails,
} from "node-opcua-client";
import { Resource, Tool } from "@modelcontextprotocol/sdk/types.js";

import { browseAllReferences, typeDefinitionOf } from "./browse.js";
import { WARM_UP_WAIT_MS } from "./config.js";
import {
  OpcuaConnection,
  isConnectionError,
  notConnectedMessage,
  stillConnectingMessage,
} from "./connection.js";
import { CONTRACT, type ToolSpec } from "./contract.js";
import { NodeMetadata, withinRange, type AnalogInfo } from "./node-metadata.js";
import { AuditSink, AuditWriteError, buildRecord, operatorId } from "./audit.js";
import { ContractRefusal, message } from "./errors.js";
import {
  MAX_HISTORY_VALUES,
  MAX_SUBSCRIPTIONS,
  aggregateIntervals,
  checkRequestBounds,
  chunked,
  eventBufferSize,
  historyValues,
} from "./limits.js";
import {
  Completeness,
  bufferCompleteness,
  drainCompleteness,
  historyCompleteness,
  traversalCompleteness,
} from "./completeness.js";
import { continues, releaseContinuationPoint } from "./history.js";
import {
  ServerOperationLimits,
  UNSTATED,
  browseChunk,
  readChunk,
  readOperationLimits,
  writeLimit,
} from "./operation-limits.js";
import { notice } from "./notices.js";
import { validateArguments } from "./validation.js";
import { ServerStatusRecord, disconnectedStatus, readServerStatus } from "./diagnostics.js";
import { toDate } from "./dates.js";
import {
  DEFAULT_NOTIFIER,
  EVENT_DEFAULTS,
  EventRecord,
  EventSubscriptions,
  alarmAction,
  droppedEventsMessage,
  listActiveAlarms,
  readEventHistory,
} from "./events.js";
import { canonicalNodeId } from "./node-ids.js";
import { historyData, toHistoryRecords, toIsoUtc, variantToJson } from "./records.js";
import { describeSecurity, securityConfig } from "./security.js";
import {
  SubscribeOptions,
  SubscriptionFilter,
  SubscriptionManager,
  SubscriptionRecord,
  resolveFilter,
  unknownSubscriptionsMessage,
} from "./subscriptions.js";
import {
  ToolPolicy,
  asNumber,
  controlGate,
  formatNumber,
  pairsAt,
  serverIdentityRecord,
  toolPolicy,
  valuesAt,
  type ValueBound,
} from "./policy.js";
import { convertForVariant } from "./variant-codec.js";
import { builtInType, guessVariant } from "./method-arguments.js";
import { isGood } from "./status.js";
import { randomBytes } from "crypto";

/** The standard Root and Objects folders, which a browse path is written from. */
const ROOT_FOLDER = "ns=0;i=84";

/** The HasSubtype reference type (ns=0), which links a DataType to its parent. */
const HAS_SUBTYPE = 45;

/** One node's reading (resultShapes.nodeValues). */
interface NodeValueRecord {
  node_id: string;
  value: unknown;
  data_type: string | null;
  status: string;
  source_timestamp: string | null;
  server_timestamp: string | null;
  engineering: AnalogInfo | null;
}

/** One node found by a browse (resultShapes.nodeRefs.nodes). */
interface NodeRefRecord {
  node_id: string;
  browse_name: string;
  node_class: string;
  parent_node_id: string;
  data_type: string | null;
  value: unknown;
  description: string | null;
  type_definition: string | null;
}

/** One attempted write (resultShapes.writeResults). */
interface WriteResultRecord {
  node_id: string;
  status: string;
  error: string | null;
}

/** One requested write, as `write_opcua_nodes` receives it. */
interface WriteRequest {
  node_id: string;
  value: unknown;
  data_type?: string;
}

/** An error's message, however it arrived. */
function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function clampInt(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(Math.trunc(value), high));
}

/** The OPC UA name of a variant's data type: "Double", "Boolean", "Int32". */
function dataTypeName(variant: Variant | null | undefined): string | null {
  const dataType = variant?.dataType;
  if (dataType === undefined || dataType === null || dataType === DataType.Null) return null;
  return DataType[dataType] ?? null;
}

/** The OPC UA name behind a DataType *attribute*, which is a NodeId, not an enum. */
function dataTypeNameFromNodeId(value: unknown): string | null {
  const identifier = (value as { value?: unknown } | null)?.value;
  if (typeof identifier !== "number") return null;
  return DataType[identifier] ?? null;
}

/** A DataType named as the contract names it, or a readable refusal. */
function namedDataType(name: string): DataType {
  const dataType = DataType[name as keyof typeof DataType];
  if (typeof dataType !== "number") {
    throw new Error(`Unknown data_type "${name}"`);
  }
  return dataType;
}

/** Whether a browse-path segment names this BrowseName.
 *
 * `2:Sensors` matches only namespace 2; a bare `Sensors` matches the name in
 * whatever namespace it is in. The bare form is what someone types when they
 * know what a thing is called and not which namespace it was loaded into —
 * which is the entire reason `browse_path` exists.
 */
function browseNameMatches(segment: string, namespaceIndex: number, name: string | null): boolean {
  const separator = segment.indexOf(":");
  if (separator > 0) {
    const index = Number(segment.slice(0, separator));
    if (Number.isInteger(index)) {
      return index === namespaceIndex && segment.slice(separator + 1) === name;
    }
  }
  return segment === name;
}

/** A node's present reading as a number, or null if there is not one to compare. */
function currentNumber(dataValue: DataValue | undefined): number | null {
  if (!dataValue || !isGood(dataValue.statusCode)) return null;
  return asNumber(variantToJson(dataValue.value));
}

/** Refuse a value outside the range the OPC UA server itself published.
 *
 * This is the bound that needs no policy file at all, and it is the better one:
 * the plant declared what the node is expected to hold in normal operation
 * (Part 8 §5.3), so nobody has to retype it into a JSON file and keep it in
 * step. An operator's `min`/`max` is checked separately, by the policy layer,
 * and both apply — so a policy file can only ever *narrow* what the equipment
 * already allows, never widen it.
 *
 * A non-numeric value is left alone: the variant codec is what judges whether a
 * string or a boolean belongs on this node, and it says so better than a range
 * comparison could.
 */
export function checkEuRange(nodeId: string, value: unknown, info: AnalogInfo | null): void {
  const range = info?.eu_range;
  if (!range) return;
  const number = asNumber(value);
  if (number === null || withinRange(range, number)) return;
  throw new ContractRefusal(
    message("valueOutOfRange", {
      value: formatNumber(number),
      node_id: nodeId,
      low: formatNumber(range.low),
      high: formatNumber(range.high),
      unit: info?.unit ? ` ${info.unit}` : "",
      source: "the OPC UA server's own EURange",
    })
  );
}

/** Refuse a move larger than the operator allows in one write.
 *
 * Scalars only. An array write has no single "how far did it move", and guessing
 * one — the largest element-wise delta, say — would be a rule nobody could
 * predict from the policy file, so it is refused instead.
 */
export function checkMaxChange(
  nodeId: string,
  value: unknown,
  limit: number,
  dataValue: DataValue | undefined
): void {
  const present = currentNumber(dataValue);
  if (present === null) {
    const reason = !dataValue
      ? "it could not be read"
      : !isGood(dataValue.statusCode)
        ? dataValue.statusCode.name
        : "the node returned no usable value";
    throw new ContractRefusal(message("currentValueUnreadable", { node_id: nodeId, reason }));
  }
  const wanted = asNumber(value);
  if (wanted === null) {
    throw new ContractRefusal(
      message("valueNotComparable", {
        node_id: nodeId,
        value: JSON.stringify(value) ?? String(value),
      })
    );
  }
  const change = Math.abs(wanted - present);
  if (change > limit) {
    throw new ContractRefusal(
      message("valueChangeTooLarge", {
        node_id: nodeId,
        current: formatNumber(present),
        value: formatNumber(wanted),
        change: formatNumber(change),
        limit: formatNumber(limit),
      })
    );
  }
}

/** One node's reading as a canonical record (resultShapes.nodeValues).
 *
 * The value goes through the *shared* codec, so a Boolean is `true` on both
 * runtimes rather than `true` here and `True` there, and an Int64 is a number
 * or a numeric string rather than node-opcua's `[high, low]` pair. Reading used
 * to stringify natively and so diverged by construction — the one thing
 * `value-encoding.json` exists to prevent, just outside its reach.
 */
export function toNodeValueRecord(
  nodeId: string,
  dataValue: DataValue | undefined,
  engineering: AnalogInfo | null = null
): NodeValueRecord {
  const good = dataValue !== undefined && isGood(dataValue.statusCode);
  return {
    node_id: canonicalNodeId(nodeId),
    value: good ? variantToJson(dataValue?.value) : null,
    data_type: good ? dataTypeName(dataValue?.value) : null,
    // An absent status code means Good in OPC UA, so name it rather than null.
    status: dataValue?.statusCode?.name ?? "Good",
    source_timestamp: toIsoUtc(dataValue?.sourceTimestamp),
    server_timestamp: toIsoUtc(dataValue?.serverTimestamp),
    // What the plant says this number means. null for most nodes, because only
    // an AnalogItemType publishes it — but on the ones that do it is the
    // difference between "51.75" and "51.75 °C, normal range 0 to 150".
    engineering,
  };
}

/** Where a truncated history read resumes, or null when its arguments cannot say.
 *
 * Only a forward read can be continued with `start_time`: a start was given and
 * the range runs up from it. Without one, an OPC UA server reads backwards from
 * the end, newest first (Part 11 §6.4.3.2), and the rest of the answer is then
 * *older* records — which no start_time asks for. The last record's own
 * timestamp is the resume point, inclusive, so a boundary record repeats rather
 * than being lost. `_forward_from` in server.py is the other half.
 */
function forwardFrom(
  start: Date | undefined,
  end: Date | undefined,
  last: string | null | undefined
): string | null {
  if (!start || (end && end.getTime() <= start.getTime())) return null;
  return typeof last === "string" ? last : null;
}

/** A text block for a reader that only has the text, repeating `completeness`.
 *
 * Only for a loss the caller did not choose — this server's cap, the OPC UA
 * server's, or a full buffer — never for a count the caller asked for and got:
 * telling someone who asked for 10 readings that there may be more would be
 * noise on every small read. `completeness` reports that case on its own.
 */
function withNotice<T extends { content: Array<{ type: string; text: string }> }>(
  result: T,
  text: string | null
): T {
  if (text === null) return result;
  return { ...result, content: [...result.content, { type: "text", text }] };
}

/** History records (resultShapes.historyRecords), and whether they are all of them.
 *
 * One text block per record, as FastMCP splits the Python server's list, plus
 * the `completeness` object beside `result` in structuredContent (issue #137).
 * The notice for a capped read predates that object and is kept for text-only
 * readers, and fires exactly when it always did: when this server's own cap was
 * reached, which is `contractLimit`.
 */
function historyResult(records: unknown[], completeness: Completeness, capNotice: string) {
  let text: string | null = null;
  if (completeness.reasons.includes("contractLimit")) {
    // The cap, not the count returned: an event-history read reaches its cap on
    // what the server sent, and `severity_min` may have kept fewer.
    text = notice(capNotice, { count: completeness.limit ?? completeness.returned });
  } else if (completeness.reasons.includes("serverLimit")) {
    text = notice("serverTruncated", { count: completeness.returned });
  }
  return withNotice(recordBlocks(records, completeness), text);
}

/** The same framing for the subscription family (resultShapes.subscriptionRecords).
 *
 * Each record carries its own ring buffer's `dropped`; `completeness` totals them
 * so one field answers for the whole result.
 */
function subscriptionResult(records: SubscriptionRecord[]) {
  return recordBlocks(records, bufferCompleteness(records));
}

/** And for the event family (resultShapes.eventRecords).
 *
 * `list_active_alarms` passes no completeness: a refresh that does not finish is
 * an error, never a shorter list, so its answer is whole or it is not given.
 */
function eventResult(records: EventRecord[], completeness?: Completeness) {
  return recordBlocks(records, completeness);
}

/** A result that is one object rather than a list of records.
 *
 * One text block and a `result` that is the object itself. Used by every shape
 * where a list would be a lie about the answer's structure: a browse has one
 * `truncated` flag for the whole walk, a method call has one result, and a
 * status report is one report. The Python server frames these identically.
 */
function objectResult(record: unknown, completeness?: Completeness) {
  return {
    content: [{ type: "text", text: JSON.stringify(record, null, 2) }],
    structuredContent: completeness ? { result: record, completeness } : { result: record },
  };
}

/** The diagnostics report (resultShapes.serverStatus). */
function statusResult(status: ServerStatusRecord) {
  return objectResult(status);
}

function recordBlocks(records: unknown[], completeness?: Completeness) {
  return {
    content: records.map((record) => ({
      type: "text",
      text: JSON.stringify(record, null, 2),
    })),
    // Beside `result`, never inside it: `result` stays the array every existing
    // client already reads, and a record list that sometimes ends in something
    // that is not a record would be worse than the prose it replaces.
    structuredContent: completeness ? { result: records, completeness } : { result: records },
  };
}

/** What tools/list advertises a tool returns: `result`, and `completeness` if it
 *  can be partial. `PolicyMCPServer.list_tools` in server.py builds the same one. */
function outputSchema(tool: ToolSpec): Tool["outputSchema"] {
  if (!tool.resultShape) return undefined;
  if (!tool.reportsCompleteness) {
    return {
      type: "object",
      properties: { result: CONTRACT.resultShapes[tool.resultShape] },
      required: ["result"],
      additionalProperties: false,
    } as Tool["outputSchema"];
  }
  return {
    type: "object",
    properties: {
      result: CONTRACT.resultShapes[tool.resultShape],
      completeness: CONTRACT.completeness.schema,
    },
    required: ["result", "completeness"],
    additionalProperties: false,
  } as Tool["outputSchema"];
}

/** What a call was aimed at, for a message a human will read.
 *
 * The same `guard` the audit record and the policy read, so the three cannot
 * name different things. Targets only — never the values, for the same reason
 * `auditDecision` withholds them.
 */
export function describeTargets(tool: ToolSpec, args: Record<string, unknown>): string {
  const targets = auditTargets(tool, args);
  const parts = Object.entries(targets).map(
    ([key, value]) => `${key}=${Array.isArray(value) ? value.join(", ") : String(value)}`
  );
  return parts.length > 0 ? parts.join("; ") : "unknown";
}

/** What a control call was aimed at, for the audit record.
 *
 * Derived from the tool's own `guard`, not from a chain on tool *names*. That
 * chain was the last one left after the policy layer stopped keying off names,
 * and it broke silently the moment the tools were renamed: every write logged
 * `decision: "allowed"` with no targets at all, which is an audit trail that
 * records that *something* was permitted without recording what. Reading the
 * same declaration the policy authorises from means the two can no longer
 * disagree about which arguments matter.
 */
function auditTargets(tool: ToolSpec, args: Record<string, unknown>): Record<string, unknown> {
  const guard = tool.guard;
  if (!guard) return {};
  const record: Record<string, unknown> = {};

  const nodeIds = (guard.nodeIdPaths ?? []).flatMap((path) => valuesAt(args, path));
  if (nodeIds.length > 0) record.node_ids = nodeIds;

  const methods = (guard.methodPaths ?? []).map(({ objectPath, methodPath }) => ({
    object_node_id: valuesAt(args, objectPath)[0] ?? null,
    method_node_id: valuesAt(args, methodPath)[0] ?? null,
  }));
  if (methods.length > 0) Object.assign(record, methods[0]);

  for (const path of guard.auditPaths ?? []) {
    // Only what is present: an absent optional argument is not a target, and
    // recording it as null would make every acknowledgement look half-specified.
    const [value] = valuesAt(args, path);
    if (value !== undefined) record[path] = value;
  }
  return record;
}

/** Write one line of the control audit trail.
 *
 * Only `control` and `alarm-action` tools: an audit trail that also recorded
 * every read would bury the four lines anyone is looking for.
 *
 * Never the *values* being written, only the targets. A setpoint is process
 * data, and this stream is the one an MCP client shows the user and a log
 * collector ships off the machine.
 *
 * Throws `AuditWriteError` when the record did not land; what that means for the
 * call is decided by `auditPermission` and `auditAfter`.
 */
function auditDecision(
  sink: AuditSink,
  policy: ToolPolicy,
  connection: OpcuaConnection,
  name: string,
  args: Record<string, unknown>,
  decision: "allowed" | "denied" | "failed" | "completed",
  callId: string,
  attempt: number,
  reason?: string
): void {
  const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
  if (!tool || !["control", "alarm-action"].includes(tool.accessClass)) return;
  sink.write(
    buildRecord({
      timestamp: new Date().toISOString(),
      call_id: callId,
      attempt,
      endpoint: connection.endpointUrl,
      session: connection.sessionId,
      session_generation: connection.sessionGeneration,
      // null when OPCUA_OPERATOR_ID is unset, which is honest: this server has no
      // notion of who is calling, and a name nothing verified would be worse
      // than none.
      operator_label: operatorId(),
      ...sink.identity(),
      profile: policy.config.profile,
      // What let control through, or kept it out: `secured` for a verified
      // server, or which lab override was in force. An override that shows up
      // only in a startup line nobody kept is an override nobody can audit.
      control: controlGate(policy.config),
      tool: name,
      decision,
      targets: auditTargets(tool, args),
      reason: reason ?? null,
    })
  );
}

/** Record `allowed` before anything is sent — or refuse the call.
 *
 * The fail-closed half (#146): a control call whose permission could not be made
 * durable never reaches the plant. Reads never get here, because they are not
 * audited, so an audit outage does not take monitoring down with it.
 */
function auditPermission(
  sink: AuditSink,
  policy: ToolPolicy,
  connection: OpcuaConnection,
  name: string,
  args: Record<string, unknown>,
  callId: string,
  attempt: number
): void {
  try {
    auditDecision(sink, policy, connection, name, args, "allowed", callId, attempt);
  } catch (error) {
    if (!(error instanceof AuditWriteError)) throw error;
    console.error(`AUDIT FAILURE: refusing ${name} (call ${callId}): ${error.message}`);
    throw new ContractRefusal(message("auditUnavailable", { tool: name, reason: error.message }));
  }
}

/** Record a denial or an outcome, reporting rather than throwing if it is lost.
 *
 * Fail-closed is decided at `auditPermission`, before dispatch. A denial is
 * refused whether or not its record lands. An outcome is recorded after the call
 * reached the plant, and turning a write that happened into a reported failure
 * would invite the model to send it again — so a sink that refuses it is
 * reported on stderr, and the next control call's `allowed` record is what
 * refuses the next call.
 */
function auditAfter(...args: Parameters<typeof auditDecision>): void {
  try {
    auditDecision(...args);
  } catch (error) {
    if (!(error instanceof AuditWriteError)) throw error;
    const [, , , name, , decision, callId] = args;
    console.error(
      `AUDIT FAILURE: the ${decision} record for ${name} (call ${callId}) was not written: ${error.message}`
    );
  }
}

/** An id for one tool call, to tie its audit lines together.
 *
 * Every control call writes two lines — `allowed` before it, then `completed` or
 * `failed` after — and without this there was nothing linking them. Both runtimes
 * serve calls concurrently, so two overlapping writes produced four interleaved
 * lines and no way to say which pairs; where the targets happened to match (the
 * same node written twice) they were not even distinguishable by content. For a
 * trail whose purpose is "which control call reached the plant and did it land",
 * that was the one missing field.
 *
 * Random rather than a counter: it never needs to be meaningful or ordered, only
 * unique within a process, and a counter would invite reading it as a total. The
 * Python half uses `secrets.token_hex(8)`, which is the same sixteen hex digits.
 */
export function newCallId(): string {
  return randomBytes(8).toString("hex");
}

export class OpcuaTools {
  private aggregateFunctions: string[] = [];
  /** What the connected server reports it can do; null until first probed.
   *  Dropped on every session change — a restarted server may answer
   *  differently, and a stale yes is a tool that fails instead of being hidden. */
  private capabilities: Set<string> | null = null;
  private readonly subs = new SubscriptionManager();
  private readonly events = new EventSubscriptions();
  /** What each node published about its own number, for the life of one session. */
  private readonly metadata = new NodeMetadata();
  /** The startup warm-up, once started, and when requests stop waiting for it. */
  private warmUpPromise: Promise<void> | null = null;
  private warmUpDeadline = 0;
  /** How long `awaitWarmUp` gives the warm-up, from its start. A field so the
   *  unit tests can shorten it; the server always uses the constant. */
  warmUpWaitMs = WARM_UP_WAIT_MS;
  /** What the connected server says one service call may carry; see
   *  operation-limits.ts. Probed with the capabilities, forgotten with them. */
  private serverLimits: ServerOperationLimits = UNSTATED;

  constructor(
    private readonly conn: OpcuaConnection,
    private readonly policy: ToolPolicy = toolPolicy(),
    private readonly audit: AuditSink = new AuditSink()
  ) {
    // A rebuilt connection is a new session, and an OPC UA subscription belongs
    // to the session that created it. Without this, a server restart would leave
    // every `subscribe_opcua_nodes` handle the agent holds silently dead.
    this.conn.onSessionReplaced = async (session) => {
      // A new session may be a restarted server with different capabilities, and
      // it is the only moment the answer can have changed — which is what lets
      // `listTools` stop waiting on a socket.
      this.capabilities = null;
      this.serverLimits = UNSTATED;
      this.metadata.serverLimits = UNSTATED;
      // A new session may be a restarted server, whose nodes are not necessarily
      // the nodes the old ids named. What each one said about its unit and its
      // range was true of the session that said it.
      this.metadata.forget();
      await this.subs.reattach(session);
      await this.events.reattach(session);
      // With the session in hand, not through the connection: this runs inside
      // `reconnect()`, and a probe that called `ensureConnection()` from here
      // would re-enter the connect path it is standing in.
      await this.probeCapabilities(session).catch(() => undefined);
    };
  }

  /** Open the first connection and probe it. Started by `startWarmUp`.
   *
   * The Python runtime does this in its lifespan and this runtime did not — it
   * got its first connection from whichever `tools/list` happened to arrive
   * first, which is precisely the coupling #83 removed. Without a warm-up the
   * first catalogue would now always be the core tools, even against a plant
   * that is up.
   *
   * Best-effort and never fatal: an MCP client starts this server when *it*
   * starts, which may be long before the plant network is reachable.
   * `get_server_status` reports what is wrong in the meantime, and every tool
   * call retries.
   */
  async warmUp(): Promise<void> {
    await this.conn.ensureConnection().catch(() => undefined);
    if (!this.capabilities) await this.probeCapabilities().catch(() => undefined);
  }

  /** Start the warm-up without waiting for it, once. Returns it, for tests.
   *
   * The server used to await `warmUp()` before opening the MCP transport, so
   * nothing — not `initialize`, not `get_server_status` — was answered until the
   * first connection round had run its course. Bounded, that is the whole
   * configured backoff; with `OPCUA_RECONNECT_MAX_RETRY=-1` it was a server that
   * never started (#136). The transport now opens at once and this runs beside
   * it.
   *
   * What awaiting it bought is kept, within a bound. Requests served *during*
   * the warm-up are why it had moved in front of the transport: a catalogue
   * listed then was the core tools only, and a status read then said "not
   * connected", against a plant that was up. So those two wait for it — see
   * `awaitWarmUp` — and a tool call that needs a session joins its round through
   * `ensureConnection`, as it always did.
   */
  startWarmUp(): Promise<void> {
    if (!this.warmUpPromise) {
      this.warmUpDeadline = Date.now() + this.warmUpWaitMs;
      this.warmUpPromise = this.warmUp();
    }
    return this.warmUpPromise;
  }

  /** Wait for the warm-up, and any other connection round in flight, unbounded.
   *
   * For a tool call, before it is authorized and audited. Both read the
   * session: the policy resolves `nsu=` allowlist entries through the namespace
   * mapping bound on connect, and the audit record names the session the call
   * rides on (#105, #107). When the warm-up ran before the transport opened,
   * every call found it finished; a call that arrives during it now waits for
   * it, where before it would have been refused a URI-pinned node and audited
   * against no session at all. Bounded by the round itself, which always ends.
   * Never starts a round: a call made while disconnected connects after it is
   * authorized, as it always has.
   */
  private async awaitConnectionInFlight(): Promise<void> {
    if (this.warmUpPromise) await this.warmUpPromise;
    await this.conn.settled();
  }

  /** Wait for the startup warm-up, but never past `warmUpWaitMs` from its start.
   *
   * Against a plant that is up the warm-up finishes well inside the window, so
   * the first `tools/list` carries the whole catalogue and the first status is a
   * connected one. Against a plant that is down it can take the whole round, and
   * a server that is to be diagnosable has to answer before then — from what it
   * knows. The Python server's `ServerState.await_warm_up` is the same wait.
   */
  private async awaitWarmUp(): Promise<void> {
    const remaining = this.warmUpDeadline - Date.now();
    if (!this.warmUpPromise || remaining <= 0) return;
    let timer: NodeJS.Timeout | undefined;
    await Promise.race([
      this.warmUpPromise,
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, remaining);
      }),
    ]);
    clearTimeout(timer);
  }

  /** Tear down every OPC UA subscription this server created.
   *
   * Called before the session is closed, on every shutdown path. Closing the
   * session alone would leave the OPC UA server publishing to nobody until the
   * subscription's lifetime expired. Event subscriptions are subscriptions too,
   * and cost the server the same until they expire.
   */
  async shutdown(): Promise<void> {
    await this.subs.closeAll();
    await this.events.closeAll();
  }

  // Delegations that keep the tool bodies below identical to their previous
  // form as methods of the old monolithic server class.
  private get session(): ClientSession | null {
    return this.conn.session;
  }

  private ensureConnection(): Promise<void> {
    return this.conn.ensureConnection();
  }

  private serverCapabilitiesAggregateFunctions(on?: ClientSession): Promise<string[]> {
    return this.conn.serverCapabilitiesAggregateFunctions(on);
  }

  private accessHistoryDataCapability(on?: ClientSession): Promise<boolean> {
    return this.conn.accessHistoryDataCapability(on);
  }

  private accessHistoryEventsCapability(on?: ClientSession): Promise<boolean> {
    return this.conn.accessHistoryEventsCapability(on);
  }

  /** Read what the connected OPC UA server can do, off the session we already have.
   *
   * Never connects. Both probes run against a live session or not at all, so a
   * failure is not fatal — the core tools are offered regardless, and the
   * optional ones appear once a session exists and has been probed.
   */
  private async probeCapabilities(on?: ClientSession): Promise<Set<string>> {
    const available = new Set<string>();
    const session = on ?? this.session;
    if (!session) {
      // Deliberately not cached. "No session yet" is not "this server supports
      // nothing", and caching it as though it were is what made a request served
      // during the warm-up poison the answer for the rest of the process.
      this.aggregateFunctions = [];
      return available;
    }
    const historyOk = await this.accessHistoryDataCapability(session);
    const historyEventsOk = await this.accessHistoryEventsCapability(session);
    // Read with the capabilities because it changes when they do — on a new
    // session — and a tool that needs it can then use it without a round trip.
    this.serverLimits = await readOperationLimits(session);
    this.metadata.serverLimits = this.serverLimits;
    this.aggregateFunctions = await this.serverCapabilitiesAggregateFunctions(session);

    // A tool gated on capabilities is offered when the server reports *any* of
    // them. `read_opcua_history` lists both: a server with only aggregates can
    // still answer an aggregate read, and gating it on `history` alone would
    // hide the one thing such a server is good at.
    if (historyOk) available.add("history");
    if (historyEventsOk) available.add("historyEvents");
    if (this.aggregateFunctions.length > 0) available.add("aggregate");
    this.capabilities = available;
    return available;
  }

  /** The server's stated operation limits, probing once if need be.
   *
   * Every tool that sends a batch asks here rather than reading the field, for
   * the reason `capabilitiesMet` probes: a process that started while the plant
   * was down has not asked yet, and "not asked" must not be read as "no limit".
   */
  private async operationLimits(): Promise<ServerOperationLimits> {
    if (!this.capabilities) await this.probeCapabilities();
    return this.serverLimits;
  }

  /** Whether a tool's capability gate is satisfied, probing once if need be.
   *
   * Called from `callTool` as well as from `listTools`, because catalog
   * filtering is not enforcement: a client may hold a tools/list from when the
   * server still reported HistoricalAccess, and this runtime used to let that
   * call straight through to node-opcua while the Python one refused it by name.
   * By the time `callTool` asks, a session has been ensured, so a cold cache
   * here probes rather than guesses.
   */
  private async capabilitiesMet(tool: ToolSpec): Promise<boolean> {
    if (tool.capabilities.length === 0) return true;
    const available = this.capabilities ?? (await this.probeCapabilities());
    return tool.capabilities.some((capability) => available.has(capability));
  }

  /** The advertised tool list: the contract, gated by runtime capabilities.
   *
   * Deliberately does no network I/O. This used to call `ensureConnection()`
   * before probing, so against an unreachable plant every tools/list sat through
   * node-opcua's whole `connectionStrategy` backoff — and clients list at
   * session start, which is exactly when a plant that is down is most likely to
   * be down.
   *
   * The capabilities are probed where they can change instead: on every
   * (re)connect, through `onSessionReplaced`. Convergence is unchanged — a
   * client that listed while the plant was down sees the core tools, any tool
   * call brings the connection up and re-probes, and the next tools/list carries
   * the full catalogue. What is gone is only the waiting.
   *
   * No `notifications/tools/list_changed` is sent, here or on the Python
   * runtime, for the same reason no `notifications/resources/updated` is — see
   * docs/architecture.md.
   */
  async listTools(): Promise<Tool[]> {
    // The one wait this does, and it is on the startup warm-up, bounded — never
    // on a connection of its own.
    await this.awaitWarmUp();
    const available = this.capabilities ?? new Set<string>();
    const aggregateOk = available.has("aggregate");

    const tools = this.policy
      .visibleTools(CONTRACT.tools)
      .filter(
        (tool) =>
          tool.capabilities.length === 0 ||
          tool.capabilities.some((capability) => available.has(capability))
      )
      .map((tool) => ({
        name: tool.name,
        description: tool.description,
        inputSchema: this.advertisedSchema(tool, aggregateOk),
        annotations: tool.annotations,
        outputSchema: outputSchema(tool),
      })) satisfies Tool[];

    return tools;
  }

  /** A tool's input schema as advertised, with capability-gated properties removed.
   *
   * Capability gating moved down a level when the history and aggregate tools
   * merged: `read_opcua_history` is advertised whenever the server reports
   * HistoricalAccess, and its `aggregate_function` argument appears only if the
   * server also advertises aggregates — with that server's *own* function list
   * named in the description. An argument the server cannot honour is therefore
   * not merely documented as unsupported; it is not offered, which is the same
   * property tool-level gating had and strictly more informative, because the
   * list is the live one.
   */
  private advertisedSchema(tool: ToolSpec, aggregateOk: boolean): Tool["inputSchema"] {
    if (!tool.inputSchema?.properties?.aggregate_function) return tool.inputSchema;

    const schema = JSON.parse(JSON.stringify(tool.inputSchema));
    if (!aggregateOk) {
      delete schema.properties.aggregate_function;
      delete schema.properties.processing_interval;
      return schema;
    }
    schema.properties.aggregate_function.description += `, one of: ${this.aggregateFunctions.join(", ")}`;
    return schema;
  }

  /** The advertised resource list: taken straight from the contract. */
  listResources(): Resource[] {
    return CONTRACT.resources.map((r) => ({
      uri: r.uri,
      name: r.name,
      description: r.description,
      mimeType: r.mimeType,
    }));
  }

  /** Serve a resources/read request.
   *
   * Deliberately does not touch the OPC UA server: this reports what the
   * subscriptions have already delivered, so it stays readable — and honest —
   * even while the connection is down.
   */
  readResource(uri: string) {
    const resource = CONTRACT.resources.find((r) => r.uri === uri);
    if (!resource) {
      throw new Error(`Unknown resource: ${uri}`);
    }
    return {
      contents: [
        {
          uri: resource.uri,
          mimeType: resource.mimeType,
          text: JSON.stringify({ [resource.body.recordsKey]: this.subs.list() }, null, 2),
        },
      ],
    };
  }

  /** Serve a tools/call request: authorize it, then run it on a live session. */
  async callTool(request: { params: { name: string; arguments?: Record<string, unknown> } }) {
    const { name } = request.params;
    const args = request.params.arguments ?? {};
    const callId = newCallId();
    let authorized = false;
    // How the audit trail sees this call while it is in flight.
    //
    // `attempt` is which *physical* attempt the lines below are about: one call
    // can reach the plant twice — the session dies, the connection is rebuilt,
    // the request is re-sent — and `recover` bumps it so `completed` and
    // `failed` say which attempt they describe rather than folding both into one
    // line (issue #105).
    //
    // `denied` keeps a refusal to one line rather than two. A denial has already
    // been recorded as one, and the `failed` line below would otherwise repeat
    // its reason and read as though the plant had rejected the call.
    const audit = { attempt: 1, denied: false };
    // Which session this call rides on, so recovery can tell "my session died"
    // from "someone else already replaced it".
    let session: string | null = null;

    try {
      // Shape before permission: a call that does not match the contract is not a
      // call this server can reason about, and the policy layer reads the very
      // arguments checked here to decide what a write is aimed at. There was no
      // check at all on this runtime — the low-level MCP `Server` does not
      // validate against the advertised `inputSchema`, and `dispatch` cast
      // straight off the wire — so a malformed call reached node-opcua as
      // whatever the client sent.
      const spec = CONTRACT.tools.find((candidate) => candidate.name === name);
      if (!spec) {
        throw new Error(message("unknownTool", { tool: name }));
      }

      try {
        // Inside the audited block, as on the Python runtime: a control call
        // refused for its size or its shape is still a control attempt, and is
        // recorded as `denied`. This one checked both first and so left no line
        // at all (#157).
        //
        // Size before shape: the validator's work grows with the request, and
        // this stops at the first thing out of bounds (issue #139). See limits.ts.
        checkRequestBounds(name, args);
        validateArguments(name, spec.inputSchema, args);
        // Before the policy and the audit trail read the session, not merely
        // before the request goes out. `get_server_status` is the exception: it
        // reports on the connection, and waits for the warm-up only boundedly.
        if (name !== "get_server_status") await this.awaitConnectionInFlight();
        // This is the security boundary. Filtering tools/list improves the
        // model's choices, but clients cache catalogs and may call a previously
        // visible tool directly, so authorize again before touching the OPC UA
        // network.
        this.policy.authorize(name, args);
      } catch (error) {
        auditAfter(
          this.audit,
          this.policy,
          this.conn,
          name,
          args,
          "denied",
          callId,
          1,
          error instanceof Error ? error.message : String(error)
        );
        throw error;
      }
      auditPermission(this.audit, this.policy, this.conn, name, args, callId, 1);
      authorized = true;
      // The one tool that must answer while the connection is down: it exists to
      // say so. Everything below needs a session first.
      if (name === "get_server_status") {
        await this.awaitWarmUp();
        return statusResult(await this.getServerStatus());
      }

      // Connecting is attempted before dispatching, so that a server that is
      // simply not there is reported as that rather than as a puzzling failure
      // from whichever tool happened to be called first.
      //
      // And before the capability gate, not after. The capability answers are
      // filled in by the reconnect callback, so a process that started while the
      // plant was unreachable still holds its startup defaults — and checking
      // them first refused `read_opcua_history` as "the server advertises none
      // of: history" without ever asking the server. Unknown is not absent
      // (issue #108).
      try {
        await this.ensureConnection();
      } catch (error) {
        throw new Error(
          notConnectedMessage(
            this.conn.endpointUrl,
            error instanceof Error ? error.message : String(error)
          )
        );
      }

      session = this.conn.sessionId;

      if (!(await this.capabilitiesMet(spec))) {
        throw new Error(
          message("capabilityMissing", { capabilities: spec.capabilities.join(", ") })
        );
      }

      let result;
      try {
        result = await this.dispatch(name, args);
      } catch (error) {
        if (!isConnectionError(error)) throw error;
        result = await this.recover(spec, args, callId, audit, session, error);
      }
      // The outcome, not only the decision. "Permitted" and "happened" are
      // different facts, and the gap between them is where a control call that
      // reached the plant and then failed lives — which is the one an operator
      // most needs to find afterwards.
      auditAfter(
        this.audit,
        this.policy,
        this.conn,
        name,
        args,
        "completed",
        callId,
        audit.attempt
      );
      return result;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      // Only for a call that got past authorization: a denial has already been
      // recorded as one, and logging it twice would double-count refusals.
      if (authorized && !audit.denied) {
        auditAfter(
          this.audit,
          this.policy,
          this.conn,
          name,
          args,
          "failed",
          callId,
          audit.attempt,
          reason
        );
      }
      // No "Error: " prefix. `isError` already says it is one, and the Python
      // runtime returns the bare message — so prefixing here made every failure
      // read two ways depending on which runtime a client had started.
      return {
        content: [{ type: "text", text: reason }],
        isError: true,
      };
    }
  }

  /** Rebuild the session a call died on, and decide what may follow it.
   *
   * A connection can die between `ensureConnection` and the call: it can only
   * report what was true a moment ago. What happens next is settled by the
   * contract's own `retryPolicy` — *not* by `annotations.idempotentHint`, which
   * both runtimes used to read for this. That annotation tells the model whether
   * calling a tool twice is meaningful; this decides whether this server may put
   * a second request on the wire after an outcome it does not know.
   * `write_opcua_nodes` carries `idempotentHint: true` and must not be re-sent:
   * Part 4 §5.11.4 lets a Write partially succeed and defines no operation order,
   * so a lost response never proved the write had not landed (issue #106).
   *
   * The connection is rebuilt whatever the policy, so the next call finds a live
   * session.
   */
  private async recover(
    spec: ToolSpec,
    args: Record<string, unknown>,
    callId: string,
    audit: { attempt: number; denied: boolean },
    session: string | null,
    error: unknown
  ) {
    const policy = spec.retryPolicy;
    console.error(
      `OPC UA call failed on a dead session; reconnecting${
        policy === "resend" ? " and retrying once" : ""
      }`
    );
    try {
      await this.conn.reconnect(session);
    } catch (rebuildFailed) {
      // The same failure the pre-dispatch path reports, worded the same way.
      // Left bare, this reached the model as whatever the client library said —
      // the same outage the call before it had described as "Not connected to the
      // OPC UA server at …: … Call get_server_status for details", so one server
      // said two things about one event depending on where in the request it
      // happened to notice.
      throw new ContractRefusal(
        notConnectedMessage(this.conn.endpointUrl, describeError(rebuildFailed))
      );
    }

    if (policy === "uncertainOutcome") {
      throw new Error(
        message("uncertainOutcome", {
          tool: spec.name,
          reason: describeError(error),
          targets: describeTargets(spec, args),
        })
      );
    }
    if (policy !== "resend") throw error;

    // Re-authorize before the second attempt, and audit it as its own.
    //
    // `reconnect` has just re-read the server's NamespaceArray and re-bound it
    // into the policy, because a server that restarted may have loaded its
    // namespaces in a different order — which is the whole reason the `nsu=`
    // allowlist form exists. So the mapping this call was authorized against is
    // not necessarily the mapping the second attempt will resolve against, and
    // re-running the check is what stops a request reaching a node nobody
    // allowed (issue #105). It touches no network.
    audit.attempt = 2;
    try {
      this.policy.authorize(spec.name, args);
    } catch (denial) {
      audit.denied = true;
      auditAfter(
        this.audit,
        this.policy,
        this.conn,
        spec.name,
        args,
        "denied",
        callId,
        2,
        denial instanceof Error ? denial.message : String(denial)
      );
      throw denial;
    }
    try {
      auditPermission(this.audit, this.policy, this.conn, spec.name, args, callId, 2);
    } catch (refusal) {
      // Refused before the second attempt went out, and recorded as nothing
      // more: a `failed` line would read as though the plant had answered.
      audit.denied = true;
      throw refusal;
    }

    return await this.dispatch(spec.name, args);
  }

  /** Run one tool. The caller has already authorized it and ensured a session. */
  private async dispatch(name: string, args: Record<string, unknown>) {
    switch (name) {
      case "read_opcua_nodes":
        return await this.readOpcuaNodes(args.node_ids as string[]);

      case "browse_opcua_nodes":
        return await this.browseOpcuaNodes({
          nodeId: args.node_id as string | undefined,
          browsePath: args.browse_path as string | undefined,
          depth: args.depth as number | undefined,
          nodeClass: args.node_class as string | undefined,
          nameFilter: args.name_filter as string | undefined,
          includeValues: args.include_values as boolean | undefined,
          maxNodes: args.max_nodes as number | undefined,
        });

      case "read_opcua_history":
        return await this.readOpcuaHistory({
          nodeId: args.node_id as string,
          start: args.start_time as string | undefined,
          end: args.end_time as string | undefined,
          numValues: (args.num_values as number) || 0,
          aggregateFunction: args.aggregate_function as string | undefined,
          processingInterval: (args.processing_interval as number) || 0,
        });

      case "read_event_history":
        return await this.readEventHistory({
          nodeId: (args.node_id as string) || DEFAULT_NOTIFIER,
          start: args.start_time as string | undefined,
          end: args.end_time as string | undefined,
          numValues: (args.num_values as number) || 0,
          severityMin: (args.severity_min as number) || EVENT_DEFAULTS.severityMin,
        });

      case "write_opcua_nodes":
        return await this.writeOpcuaNodes(args.nodes as WriteRequest[]);

      case "call_opcua_method":
        return await this.callOpcuaMethod(
          args.object_node_id as string,
          args.method_node_id as string,
          args.arguments as unknown[] | undefined
        );

      case "subscribe_opcua_nodes":
        return await this.subscribeOpcuaNodes(
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
        return await this.unsubscribeOpcuaNodes(args.subscription_ids as string[]);

      case "list_subscriptions":
        return subscriptionResult(this.subs.list());

      case "subscribe_events":
        return await this.subscribeEvents(
          (args.node_id as string) || DEFAULT_NOTIFIER,
          (args.severity_min as number) ?? EVENT_DEFAULTS.severityMin,
          (args.buffer_size as number) || EVENT_DEFAULTS.bufferSize
        );

      case "read_events":
        return this.readEvents(
          (args.node_id as string) || DEFAULT_NOTIFIER,
          (args.limit as number) || EVENT_DEFAULTS.readLimit
        );

      case "list_active_alarms":
        return await this.listActiveAlarms(
          (args.node_id as string) || DEFAULT_NOTIFIER,
          (args.timeout_seconds as number) ?? EVENT_DEFAULTS.refreshTimeoutSeconds
        );

      case "acknowledge_alarm":
        return await this.actOnAlarm(
          args.event_id as string,
          "acknowledge",
          (args.comment as string) ?? "",
          null,
          args.condition_id as string | undefined
        );

      case "act_on_alarm":
        return await this.actOnAlarm(
          args.event_id as string,
          args.action as string,
          (args.comment as string) ?? "",
          (args.shelve_duration_ms as number | undefined) ?? null,
          args.condition_id as string | undefined
        );

      default:
        throw new Error(message("unknownTool", { tool: name }));
    }
  }

  /** The `get_server_status` report: connection state, then what the server says.
   *
   * Connecting is attempted rather than assumed, so asking for the status is
   * also the cheapest way to bring a dropped connection back. A failure to
   * connect is the answer, not an error — "not connected, and here is why" is
   * exactly what the caller asked for.
   */
  private async getServerStatus(): Promise<ServerStatusRecord> {
    const endpoint = this.conn.endpointUrl;
    const security = describeSecurity(securityConfig());
    const identity = serverIdentityRecord(this.policy.config);
    // A round someone else started is not joined. Against a plant that is down
    // it runs the whole configured backoff, and this is the report of why
    // nothing is connected — the one answer that must not wait for it (#136).
    // The round carries on; asking again reports how it ended.
    if (this.conn.connecting) {
      return disconnectedStatus(
        endpoint,
        security,
        identity,
        stillConnectingMessage(endpoint, this.conn.lastErrorMessage)
      );
    }
    try {
      // Through the same retry as every other read, so that asking for the
      // status also re-establishes a session that has silently died — which is
      // exactly the moment someone asks.
      return await this.conn.withRetry(() =>
        readServerStatus(this.requireSession(), endpoint, security, identity)
      );
    } catch (error) {
      return disconnectedStatus(endpoint, security, identity, describeError(error));
    }
  }

  private requireSession(): ClientSession {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }
    return this.session;
  }

  /** One logical read, sent as consecutive Reads of at most `chunk` items.
   *
   * Sequential rather than in parallel: the chunking exists because the server
   * said how much one request may carry, and firing every chunk at once would
   * put the same load on it in a different envelope. The results are
   * concatenated in the order asked, so each node keeps its own status in its own
   * place (issue #139).
   */
  private async readValues(
    session: ClientSession,
    items: Array<{ nodeId: string; attributeId: AttributeIds }>,
    chunk: number
  ): Promise<DataValue[]> {
    const values: DataValue[] = [];
    for (const part of chunked(items, chunk)) {
      values.push(...(await session.read(part)));
    }
    return values;
  }

  // --- reading -------------------------------------------------------------

  /** `read_opcua_nodes`: the current value of one or more nodes, fully qualified.
   *
   * One `read` for the whole list, so fifty nodes cost one round trip. A node
   * the server rejects is one record with a `Bad…` status among the others —
   * promoting it to an error would discard every other node's value, which is
   * the opposite of what asking for them together is for.
   *
   * More than `limits.maxNodesPerRead` is refused by the input schema's
   * `maxItems` before this runs: a short list of readings is indistinguishable
   * from a complete one, so the list is never quietly cut. A server whose
   * MaxNodesPerRead is lower gets the list in consecutive Reads instead.
   */
  private async readOpcuaNodes(nodeIds: string[]) {
    const session = this.requireSession();
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error(message("emptyArray", { tool: "read_opcua_nodes", argument: "node_ids" }));
    }
    const chunk = readChunk(await this.operationLimits());

    try {
      const dataValues = await this.readValues(
        session,
        nodeIds.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value })),
        chunk
      );
      // Two extra round trips on a cold cache for the whole batch, none on a
      // warm one, and never a reason for the read to fail. See node-metadata.ts.
      const engineering = await this.metadata.forNodes(session, nodeIds);
      return recordBlocks(
        nodeIds.map((nodeId, index) =>
          toNodeValueRecord(nodeId, dataValues[index], engineering.get(nodeId) ?? null)
        )
      );
    } catch (error) {
      throw new Error(message("readFailed", { reason: describeError(error) }));
    }
  }

  /** `read_opcua_history`: raw stored readings, or one aggregate per interval.
   *
   * The two used to be separate tools with separate implementations of the same
   * framing. They differ in one request and share everything else, so they are
   * one tool whose `aggregate_function` argument decides which request is sent.
   */
  private async readOpcuaHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    aggregateFunction?: string;
    processingInterval: number;
  }) {
    const session = this.requireSession();
    const { nodeId, aggregateFunction } = request;

    // Outside the try, as the Python runtime has it. Inside, this refusal came
    // back wrapped as "Failed to read history of node ns=2;i=3: ..." on this
    // runtime and bare on the other — the request never reached the OPC UA
    // server, so nothing failed to be read.
    if (aggregateFunction !== undefined && request.start === undefined) {
      throw new Error(message("aggregateNeedsStart"));
    }

    try {
      if (aggregateFunction === undefined) {
        // `0` used to mean "every reading in the range", which against a node
        // historised at 100ms is a request that never returns — and the browse
        // caps beside it have always been refusals rather than tuning knobs.
        const wanted = historyValues(request.numValues);
        const start = toDate(request.start);
        const end = toDate(request.end);
        const historyReadings = await session.readHistoryValue([nodeId], start as any, end as any, {
          numValuesPerNode: wanted,
        });
        if (historyReadings.length !== 1) throw new Error("Read history failed");
        const reading = historyReadings[0];
        // Good severity, not plain Good: GoodNoData is an empty range with
        // completeness complete, not a failed read (#157).
        const dataValues = historyData<DataValue>(reading, "Read history", "dataValues");
        const continued = continues(reading.continuationPoint);
        // The details `readHistoryValue` sent, so the server knows which history
        // the point belongs to.
        await releaseContinuationPoint(
          session,
          nodeId,
          reading.continuationPoint,
          new ReadRawModifiedDetails({
            startTime: start,
            endTime: end,
            numValuesPerNode: wanted,
            returnBounds: true,
            isReadModified: false,
          })
        );
        const records = toHistoryRecords(dataValues);
        return historyResult(
          records,
          historyCompleteness({
            returned: records.length,
            fetched: records.length,
            wanted,
            continuationPoint: continued,
            nextStart: forwardFrom(start, end, records.at(-1)?.timestamp),
          }),
          "historyTruncated"
        );
      }

      // Don't depend on a prior tools/list having populated the cache: a client
      // may call this tool directly after connecting. Recompute on demand.
      if (this.aggregateFunctions.length === 0) {
        this.aggregateFunctions = await this.serverCapabilitiesAggregateFunctions();
      }
      if (!this.aggregateFunctions.includes(aggregateFunction)) {
        throw new Error(
          this.aggregateFunctions.length === 0
            ? "Server does not advertise any aggregate functions"
            : `Invalid aggregate function. Supported: ${this.aggregateFunctions.join(", ")}`
        );
      }

      const start = toDate(request.start)!;
      const end = toDate(request.end) ?? new Date();
      // The number of results is decided by `processing_interval` over the range,
      // which is the whole point of asking for one — it is how to see a week
      // without transferring a week. It is still a number of records, though,
      // and a millisecond interval over a year is billions of them; so it is
      // bounded by the same cap as a raw read, and refused before it is sent.
      const intervals = aggregateIntervals(
        start.getTime(),
        end.getTime(),
        request.processingInterval
      );
      if (intervals > MAX_HISTORY_VALUES) {
        throw new ContractRefusal(
          message("tooManyIntervals", {
            tool: "read_opcua_history",
            count: intervals,
            processing_interval: formatNumber(request.processingInterval),
            limit: MAX_HISTORY_VALUES,
          })
        );
      }

      const aggregateType = AggregateFunction[aggregateFunction as keyof typeof AggregateFunction];
      const aggregated = await session.readAggregateValue(
        { nodeId },
        start as any,
        end as any,
        aggregateType,
        request.processingInterval
      );
      const dataValues = historyData<DataValue>(aggregated, "Read aggregate", "dataValues");
      const continued = continues(aggregated.continuationPoint);
      await releaseContinuationPoint(
        session,
        nodeId,
        aggregated.continuationPoint,
        new ReadProcessedDetails({
          startTime: start,
          endTime: end,
          aggregateType: [aggregateType],
          processingInterval: request.processingInterval,
        })
      );
      const records = toHistoryRecords(dataValues);
      // No count was asked for, so only the server can have cut this short. Where
      // it resumes is the interval after the last one returned, which is not a
      // timestamp this server should compute and round on the caller's behalf —
      // so there is no `continuation`, and `serverTruncated` says to narrow.
      return historyResult(
        records,
        historyCompleteness({
          returned: records.length,
          fetched: records.length,
          wanted: null,
          continuationPoint: continued,
          nextStart: null,
        }),
        "historyTruncated"
      );
    } catch (error) {
      // A refusal of the request never reached the server, so it did not fail
      // to be read — and wrapping it would say it had.
      if (error instanceof ContractRefusal) throw error;
      throw new Error(message("historyFailed", { node_id: nodeId, reason: describeError(error) }));
    }
  }

  // --- browsing ------------------------------------------------------------

  /** `browse_opcua_nodes`: list children, walk a subtree, resolve a path, search.
   *
   * One traversal serving what used to be `browse_opcua_node_children` and
   * `get_all_variables` — and, with `browsePath` and `nameFilter`, what issue
   * #11 asked two more tools for. They were two separate walks over the same
   * address space, which is how the missing continuation-point drain (#75)
   * reached both of them independently.
   *
   * Filtering never prunes the walk: an Object excluded by `nodeClass` is still
   * descended into while `depth` allows, because the thing being looked for is
   * usually *below* the structure, not in it.
   */
  private async browseOpcuaNodes(request: {
    nodeId?: string;
    browsePath?: string;
    depth?: number;
    nodeClass?: string;
    nameFilter?: string;
    includeValues?: boolean;
    maxNodes?: number;
  }) {
    const session = this.requireSession();
    const limits = CONTRACT.traversal;
    const depth = clampInt(request.depth ?? limits.defaultDepth, 0, limits.maxDepth);
    const maxNodes = clampInt(request.maxNodes ?? limits.defaultMaxNodes, 1, limits.maxNodes);
    const includeValues = request.includeValues ?? false;
    const wantedClass = request.nodeClass?.toLowerCase();
    const nameFilter = request.nameFilter?.toLowerCase();

    const root = request.browsePath
      ? await this.resolveBrowsePath(request.nodeId ?? limits.rootNodeId, request.browsePath)
      : canonicalNodeId(request.nodeId ?? limits.rootNodeId);

    const keep = (record: NodeRefRecord) =>
      (wantedClass === undefined || record.node_class.toLowerCase() === wantedClass) &&
      (nameFilter === undefined || record.browse_name.toLowerCase().includes(nameFilter));

    try {
      const found: NodeRefRecord[] = [];
      let inspected = 0;
      let truncated = false;
      let unbrowsable = false;

      // `depth: 0` is "tell me about this node and nothing else" — which is how
      // a browse_path is turned into a node id without also listing everything
      // under it.
      const rootRecord = await this.describeNode(session, root, root);
      if (depth === 0) {
        inspected = 1;
        if (keep(rootRecord)) found.push(rootRecord);
      } else {
        const queue: Array<{ nodeId: string; depth: number }> = [{ nodeId: root, depth: 0 }];
        const visited = new Set<string>([root]);

        while (queue.length > 0 && !truncated) {
          const current = queue.shift()!;
          let references;
          try {
            references = await browseAllReferences(session, current.nodeId);
          } catch (error) {
            // The root failing is the caller's problem; a node deeper in may
            // simply be one this session cannot read, and stopping the whole
            // walk for it would make a large browse hostage to its worst node.
            // It is still a gap in the answer, and `completeness` says so rather
            // than letting "could not list" pass for "has no children".
            if (current.nodeId === root) throw error;
            unbrowsable = true;
            continue;
          }

          for (const reference of references) {
            const childId = canonicalNodeId(reference.nodeId.toString());
            if (visited.has(childId)) continue;
            visited.add(childId);
            if (inspected >= maxNodes) {
              truncated = true;
              break;
            }
            inspected += 1;

            const browseName = `${reference.browseName.namespaceIndex}:${reference.browseName.name}`;
            // The built-in Server object is several hundred nodes of the server
            // describing itself, identical everywhere, and get_server_status
            // answers what anyone would browse it for.
            if (reference.browseName.name === limits.skipBrowseName) continue;

            const record: NodeRefRecord = {
              node_id: childId,
              browse_name: browseName,
              node_class: NodeClass[reference.nodeClass] ?? "Unspecified",
              parent_node_id: current.nodeId,
              data_type: null,
              value: null,
              description: null,
              type_definition: null,
            };
            if (keep(record)) found.push(record);

            // Descend through structure regardless of the class filter: what is
            // being looked for is usually below an Object, not the Object.
            if (reference.nodeClass === NodeClass.Object && current.depth + 1 < depth) {
              queue.push({ nodeId: childId, depth: current.depth + 1 });
            }
          }
        }
      }

      // Unconditional, unlike the variable detail: the type is what the record
      // *is*, not extra reading about its value, and it costs one batched
      // browse however many nodes were found.
      const serverLimits = await this.operationLimits();
      await this.fillTypeDefinitions(session, found, serverLimits);
      if (includeValues) await this.fillVariableDetail(session, found, serverLimits);
      return objectResult(
        { nodes: found, truncated, inspected },
        traversalCompleteness({ returned: found.length, truncated, maxNodes, unbrowsable })
      );
    } catch (error) {
      throw new Error(message("browseFailed", { node_id: root, reason: describeError(error) }));
    }
  }

  /** The record for one node read directly, rather than off a browse reference. */
  private async describeNode(
    session: ClientSession,
    nodeId: string,
    parentNodeId: string
  ): Promise<NodeRefRecord> {
    const [browseName, nodeClass] = await session.read([
      { nodeId, attributeId: AttributeIds.BrowseName },
      { nodeId, attributeId: AttributeIds.NodeClass },
    ]);
    if (!isGood(browseName.statusCode)) {
      throw new Error(`Browse failed with status: ${browseName.statusCode.name}`);
    }
    const name = browseName.value?.value;
    return {
      node_id: canonicalNodeId(nodeId),
      browse_name: name ? `${name.namespaceIndex}:${name.name}` : "",
      node_class: NodeClass[nodeClass.value?.value as number] ?? "Unspecified",
      parent_node_id: canonicalNodeId(parentNodeId),
      data_type: null,
      value: null,
      description: null,
      type_definition: null,
    };
  }

  /** Fill in `type_definition` for `records`, in one batched browse.
   *
   * `HasTypeDefinition` is non-hierarchical, so the traversal's own browse —
   * forward hierarchical references only, deliberately, or every node would
   * answer with its parent and its type instead of its children — never sees
   * it. It takes a second browse, and that is why this is one request for the
   * whole result rather than one per node: a 500-node walk would otherwise cost
   * 500 extra round trips to say what one already could.
   *
   * Best-effort, like the variable detail: a server that refuses this leaves the
   * field null rather than failing a browse that succeeded.
   */
  private async fillTypeDefinitions(
    session: ClientSession,
    records: NodeRefRecord[],
    serverLimits: ServerOperationLimits
  ): Promise<void> {
    if (records.length === 0) return;
    const traversal = CONTRACT.traversal;
    const descriptions = records.map((record) => ({
      nodeId: record.node_id,
      browseDirection: BrowseDirection.Forward,
      referenceTypeId: traversal.hasTypeDefinitionNodeId,
      // No subtypes: HasTypeDefinition has none, and asking for them would let
      // an unrelated reference through on a server that has invented one.
      includeSubtypes: false,
      nodeClassMask: 0,
      resultMask: 63,
    }));

    // Chunked for the same reason the property reads are: MaxNodesPerBrowse is
    // an operational limit a conformant server may enforce, and the default
    // walk already returns up to 500 nodes. A server that states a lower one
    // gets smaller chunks.
    const size = browseChunk(serverLimits, traversal.maxTypeDefinitionsPerRequest);
    for (let start = 0; start < descriptions.length; start += size) {
      let results;
      try {
        results = await session.browse(descriptions.slice(start, start + size));
      } catch {
        return;
      }
      results.forEach((result, index) => {
        records[start + index].type_definition = typeDefinitionOf(
          isGood(result.statusCode),
          (result.references ?? []).map((reference) => reference.browseName.name ?? "")
        );
      });
    }
  }

  /** Fill in value, data type and description for the Variables among `records`.
   *
   * One `read` for everything rather than three per node: a 500-node inventory
   * is otherwise 1500 round trips, which is the difference between a tool that
   * answers and one that times out on real equipment.
   */
  private async fillVariableDetail(
    session: ClientSession,
    records: NodeRefRecord[],
    serverLimits: ServerOperationLimits
  ): Promise<void> {
    const variables = records.filter((record) => record.node_class === "Variable");
    if (variables.length === 0) return;

    const reads = variables.flatMap((record) => [
      { nodeId: record.node_id, attributeId: AttributeIds.Value },
      { nodeId: record.node_id, attributeId: AttributeIds.DataType },
      { nodeId: record.node_id, attributeId: AttributeIds.Description },
    ]);
    let values;
    try {
      // Three attributes per node, so a 500-node walk is a 1500-item read — the
      // largest single request this server made, and one it used to send whole.
      values = await this.readValues(session, reads, readChunk(serverLimits));
    } catch {
      // Best-effort enrichment: the nodes were found, and reporting them
      // without their values beats failing a browse that succeeded.
      return;
    }

    variables.forEach((record, index) => {
      const [value, dataType, description] = values.slice(index * 3, index * 3 + 3);
      if (value && isGood(value.statusCode)) {
        record.value = variantToJson(value.value);
        record.data_type = dataTypeName(value.value);
      }
      if (record.data_type === null && dataType && isGood(dataType.statusCode)) {
        record.data_type = dataTypeNameFromNodeId(dataType.value?.value);
      }
      const text = description?.value?.value?.text;
      record.description = typeof text === "string" && text.length > 0 ? text : null;
    });
  }

  /** Resolve a slash-separated browse path to a node id (issue #11).
   *
   * Matched segment by segment against the browse names of each node's children,
   * rather than through TranslateBrowsePathsToNodeIds. Two reasons, and the
   * first is the deciding one:
   *
   * A RelativePath element carries a *qualified* BrowseName, so translating
   * `/Objects/Plant/Temperature` asks for those names in namespace 0 and a
   * plant's own nodes are never in namespace 0 — the server answers BadNoMatch
   * for a path that is plainly right. Someone who knows the namespace index can
   * write `2:Plant`, but then they already know more than this argument exists
   * to spare them. Matching here accepts either: a bare `Plant` matches
   * whatever namespace it is in, and an explicit `2:Plant` is honoured as
   * written.
   *
   * Second, browsing is universal where TranslateBrowsePaths is optional, so
   * both runtimes and every server behave the same way. It costs one browse per
   * segment, which for a path someone typed is a handful of round trips.
   *
   * A path that does not resolve is an error naming the segment that failed,
   * never an empty result: "no such path" and "a path to nothing" are different
   * answers, and only one of them is the caller's mistake.
   */
  private async resolveBrowsePath(startNodeId: string, browsePath: string): Promise<string> {
    const session = this.requireSession();
    const segments = browsePath.split("/").filter((segment) => segment.length > 0);
    if (segments.length === 0) {
      throw new Error(`browse_path "${browsePath}" names no elements`);
    }

    // A leading "/" is written from the Root folder, which is how a person says
    // it ("/Objects/..."); anything else is relative to node_id.
    let current = browsePath.startsWith("/") ? ROOT_FOLDER : canonicalNodeId(startNodeId);

    for (const segment of segments) {
      const references = await browseAllReferences(session, current);
      const match = references.find((reference) =>
        browseNameMatches(segment, reference.browseName.namespaceIndex, reference.browseName.name)
      );
      if (!match) {
        throw new Error(
          `browse_path "${browsePath}" does not resolve: no child "${segment}" under ${current}`
        );
      }
      current = canonicalNodeId(match.nodeId.toString());
    }
    return current;
  }

  // --- writing -------------------------------------------------------------

  /** `write_opcua_nodes`: one or more writes, each reporting its own status.
   *
   * Nodes given an explicit `data_type` skip the read-first inference entirely,
   * which is what makes a *write-only* node writable — reading it to learn its
   * type is exactly what such a node refuses (issue #9). The rest are read
   * first, in one batch, and converted to the type the server reports.
   *
   * The whole batch goes out as one Write, and that is a promise rather than an
   * accident (issue #139). A batch over the server's MaxNodesPerWrite is refused
   * here, before anything is read or sent, instead of being split: OPC UA lets
   * one Write partially succeed already, and splitting would add a failure where
   * the first part has moved the plant and the second never arrives — which
   * `uncertainOutcome` could not then describe. `limits.maxNodesPerWrite` is
   * the schema's `maxItems`, enforced before this runs.
   */
  private async writeOpcuaNodes(nodes: WriteRequest[]) {
    const session = this.requireSession();
    if (!Array.isArray(nodes) || nodes.length === 0) {
      throw new Error(message("emptyArray", { tool: "write_opcua_nodes", argument: "nodes" }));
    }
    const serverLimits = await this.operationLimits();
    const limit = writeLimit(serverLimits);
    if (nodes.length > limit) {
      throw new ContractRefusal(
        message("tooManyWritesForServer", {
          tool: "write_opcua_nodes",
          count: nodes.length,
          limit,
        })
      );
    }
    const bounds = new Map<number, ValueBound | null>(
      nodes.map((node, index) => [index, this.policy.boundFor(String(node?.node_id ?? ""))])
    );

    try {
      const results: WriteResultRecord[] = nodes.map((node) => ({
        node_id: canonicalNodeId(String(node?.node_id ?? "")),
        status: "Good",
        error: null,
      }));

      // A node needs its current value read for either of two reasons: its type
      // was not declared and has to be inferred, or it carries a `max_change`
      // bound, which is a bound on the *move* and so cannot be judged without
      // knowing where the node is now. One read covers both.
      const inferred = nodes
        .map((node, index) => ({ node, index }))
        .filter((entry) => !entry.node?.data_type);
      const needsCurrent = [
        ...new Set([
          ...inferred.map((entry) => entry.index),
          ...[...bounds].filter(([, b]) => b?.maxChange != null).map(([index]) => index),
        ]),
      ].sort((a, b) => a - b);
      const current =
        needsCurrent.length > 0
          ? await this.readValues(
              session,
              needsCurrent.map((index) => ({
                nodeId: nodes[index].node_id,
                attributeId: AttributeIds.Value,
              })),
              readChunk(serverLimits)
            )
          : [];
      const currentByIndex = new Map(
        needsCurrent.map((index, position) => [index, current[position]])
      );

      // Before anything is sent, and throwing rather than marking one record:
      // the whole batch is refused so it can never end up partially applied,
      // which is the property the identity allowlist already had.
      await this.checkWriteBounds(session, nodes, bounds, currentByIndex);

      const writes: Array<{ nodeId: string; attributeId: AttributeIds; value: DataValue }> = [];
      const writeIndices: number[] = [];

      nodes.forEach((node, index) => {
        try {
          const declared = node?.data_type ? namedDataType(node.data_type) : undefined;
          let dataType: DataType;
          let arrayType = VariantArrayType.Scalar;
          let dimensions: number[] | null = null;

          if (declared !== undefined) {
            dataType = declared;
            if (Array.isArray(node.value)) arrayType = VariantArrayType.Array;
          } else {
            const dataValue = currentByIndex.get(index);
            if (!dataValue || !isGood(dataValue.statusCode) || !dataValue.value) {
              results[index] = {
                node_id: results[index].node_id,
                status: dataValue?.statusCode?.name ?? "BadUnexpectedError",
                error:
                  "could not read the node's data type to convert the value; " +
                  "give data_type to write without reading it first",
              };
              return;
            }
            dataType = dataValue.value.dataType;
            arrayType = dataValue.value.arrayType;
            dimensions = dataValue.value.dimensions;
          }

          const value = convertForVariant(node.value, dataType, arrayType);
          writes.push({
            nodeId: node.node_id,
            attributeId: AttributeIds.Value,
            value: new DataValue({
              value: new Variant({ dataType, arrayType, dimensions, value }),
            }),
          });
          writeIndices.push(index);
        } catch (error) {
          // A value this server refuses to send at all — a ByteString over
          // limits.maxByteStringBytes — stops the batch rather than becoming
          // one node's status: nothing has been sent yet, and nothing should be.
          if (error instanceof ContractRefusal) throw error;
          results[index] = {
            node_id: results[index].node_id,
            status: "BadTypeMismatch",
            error: describeError(error),
          };
        }
      });

      if (writes.length > 0) {
        const statuses = await session.write(writes);
        statuses.forEach((statusCode, position) => {
          results[writeIndices[position]].status = statusCode.name;
        });
      }

      return recordBlocks(results);
    } catch (error) {
      // A refusal is already worded the way the contract words it, and it ends
      // in "Nothing was written". Wrapping it in "Failed to write nodes:" would
      // bury the reason under a framing that says the plant rejected the value
      // when in fact this server never sent it.
      if (error instanceof ContractRefusal) throw error;
      throw new Error(message("writeFailed", { reason: describeError(error) }));
    }
  }

  /** Refuse the whole batch if any value is outside what its node may hold.
   *
   * Two bounds, from two places, and both apply. The operator's `min`/`max` and
   * `enum` were already checked by the policy layer, before the network was
   * touched at all; what is left here is everything that needed a read — the
   * server's own `EURange`, and `maxChange`, which is a bound on the move.
   */
  private async checkWriteBounds(
    session: ClientSession,
    nodes: WriteRequest[],
    bounds: Map<number, ValueBound | null>,
    current: Map<number, DataValue | undefined>
  ): Promise<void> {
    const nodeIds = nodes.map((node) => String(node?.node_id ?? ""));
    const engineering = this.policy.config.allowOutOfRangeWrites
      ? new Map<string, AnalogInfo | null>()
      : await this.metadata.forNodes(session, nodeIds);

    nodes.forEach((node, index) => {
      const nodeId = nodeIds[index];
      const value = node?.value;
      // An array write is checked element by element. Writing [0, 9999] to a
      // node whose range stops at 100 is writing 9999 to it.
      for (const element of Array.isArray(value) ? value : [value]) {
        checkEuRange(nodeId, element, engineering.get(nodeId) ?? null);
      }
      const bound = bounds.get(index);
      if (bound?.maxChange != null) {
        checkMaxChange(nodeId, value, bound.maxChange, current.get(index));
      }
    });
  }

  /** `call_opcua_method`: run a method with arguments of the types it declares.
   *
   * The declared types come from the method's own InputArguments definition
   * (issue #10). Without it this parsed every argument float → int → string and
   * then forced `Double` or `String`, so a method expecting a Boolean or an
   * Int32 was called with the wrong type and either failed or — worse — did
   * something with a coerced value. The old heuristic survives only as the
   * fallback for a method that publishes no argument metadata.
   */
  private async callOpcuaMethod(
    objectNodeId: string,
    methodNodeId: string,
    methodArgs?: unknown[]
  ) {
    const session = this.requireSession();
    const args = methodArgs ?? [];

    try {
      const declared = await this.inputArgumentTypes(session, methodNodeId);
      const inputArguments = args.map((arg, index) => {
        const declaredType = declared[index];
        if (declaredType === undefined) return guessVariant(arg, index);
        return new Variant({
          dataType: declaredType.dataType,
          arrayType: declaredType.arrayType,
          value: convertForVariant(arg, declaredType.dataType, declaredType.arrayType),
        });
      });

      const callResult: CallMethodResult = await session.call({
        objectId: objectNodeId,
        methodId: methodNodeId,
        inputArguments,
      });
      // Good severity, not plain Good: GoodClamped or GoodLocalOverride is a call
      // that happened, and refusing it would report as failed an action the
      // plant carried out. The subcode is reported in `status` instead.
      if (!isGood(callResult.statusCode)) {
        throw new Error(`Method call failed with status: ${callResult.statusCode.name}`);
      }

      return objectResult({
        object_node_id: canonicalNodeId(objectNodeId),
        method_node_id: canonicalNodeId(methodNodeId),
        status: callResult.statusCode.name,
        outputs: (callResult.outputArguments ?? []).map((variant) => variantToJson(variant)),
      });
    } catch (error) {
      // Refused before the call was sent, so it did not fail: wrapping it in
      // "Failed to call method" would say the plant had turned it down.
      if (error instanceof ContractRefusal) throw error;
      throw new Error(
        message("methodFailed", {
          method_node_id: methodNodeId,
          object_node_id: objectNodeId,
          reason: describeError(error),
        })
      );
    }
  }

  /** The declared type of each input argument, or [] when the method publishes none.
   *
   * A declared DataType that is not itself built in (`Duration`, `UtcTime`, an
   * enumeration) is resolved to the built-in type it is encoded as, and one that
   * resolves to none throws: that is a method whose argument cannot be encoded,
   * not one that declares nothing, and guessing would send it anyway.
   */
  private async inputArgumentTypes(
    session: ClientSession,
    methodNodeId: string
  ): Promise<Array<{ dataType: DataType; arrayType: VariantArrayType }>> {
    let definition;
    try {
      definition = await session.getArgumentDefinition(methodNodeId);
    } catch {
      // Not every method publishes InputArguments, and a method with no
      // arguments has nothing to publish. Fall back rather than refuse.
      return [];
    }

    const supertypeOf = async (dataType: string): Promise<string | null> => {
      // Every inverse reference, filtered here rather than by the server:
      // python-opcua's server answers a browse filtered to HasSubtype with
      // nothing at all, and the Python runtime does the same for that reason.
      const result = await session.browse({
        nodeId: dataType,
        browseDirection: BrowseDirection.Inverse,
        resultMask: 63,
      });
      if (!isGood(result.statusCode)) return null;
      const parent = (result.references ?? []).find(
        (reference) =>
          reference.referenceTypeId.namespace === 0 &&
          reference.referenceTypeId.value === HAS_SUBTYPE
      );
      return parent ? canonicalNodeId(parent.nodeId.toString()) : null;
    };

    const declared = [];
    for (const argument of definition.inputArguments ?? []) {
      declared.push({
        dataType: await builtInType(canonicalNodeId(argument.dataType.toString()), supertypeOf),
        arrayType: argument.valueRank >= 1 ? VariantArrayType.Array : VariantArrayType.Scalar,
      });
    }
    return declared;
  }

  // --- data-change subscriptions -------------------------------------------

  private async subscribeOpcuaNodes(
    nodeIds: string[],
    options: SubscribeOptions,
    filter: SubscriptionFilter
  ) {
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error(
        message("emptyArray", { tool: "subscribe_opcua_nodes", argument: "node_ids" })
      );
    }
    // One OPC UA subscription per monitored node is what makes a single
    // unsubscribe take the whole thing down — and it is also what makes an
    // unbounded subscribe ask a PLC for one subscription per node, past whatever
    // it is willing to hold, with nothing here counting them.
    const active = this.subs.list().length;
    if (active + nodeIds.length > MAX_SUBSCRIPTIONS) {
      throw new Error(
        message("tooManySubscriptions", {
          active,
          limit: MAX_SUBSCRIPTIONS,
          wanted: nodeIds.length,
        })
      );
    }
    const session = this.requireSession();

    if (filter.deadbandType === "percent") {
      // A percent deadband is a percentage *of the node's EURange*, so a node
      // that publishes none cannot have one. Checked here, before a single
      // subscription is created, so a batch is refused whole rather than leaving
      // some nodes monitored and some not.
      const engineering = await this.metadata.forNodes(session, nodeIds);
      for (const nodeId of nodeIds) {
        if (!engineering.get(nodeId)?.eu_range) {
          throw new Error(message("percentDeadbandNeedsRange", { node_id: nodeId }));
        }
      }
    }

    const records: SubscriptionRecord[] = [];
    for (const nodeId of nodeIds) {
      // Named per node, as the Python runtime words it: a batch that fails on
      // its fourth node should say which one, not report the library's own
      // phrasing for whichever call happened to throw.
      try {
        records.push(await this.subs.subscribe(session, nodeId, options, filter));
      } catch (error) {
        throw new Error(
          message("subscribeFailed", { node_id: nodeId, reason: describeError(error) })
        );
      }
    }
    return subscriptionResult(records);
  }

  /** Cancel subscriptions, reporting each as it was at the moment it went.
   *
   * Every id is checked before any is cancelled: a list with one bad id would
   * otherwise leave the caller unable to tell which of the others had already
   * gone, and their buffered changes would be lost to a typo.
   */
  private async unsubscribeOpcuaNodes(subscriptionIds: string[]) {
    if (!Array.isArray(subscriptionIds) || subscriptionIds.length === 0) {
      throw new Error(
        message("emptyArray", {
          tool: "unsubscribe_opcua_nodes",
          argument: "subscription_ids",
        })
      );
    }
    const active = new Set(this.subs.list().map((record) => record.subscription_id));
    const unknown = subscriptionIds.filter((id) => !active.has(id));
    if (unknown.length > 0) {
      throw new Error(unknownSubscriptionsMessage(unknown));
    }

    const records: SubscriptionRecord[] = [];
    for (const id of subscriptionIds) {
      records.push(await this.subs.unsubscribe(id));
    }
    return subscriptionResult(records);
  }

  // --- events and Alarms & Conditions ------------------------------------------
  // The wording of every message below is shared with the Python server's
  // `events` tools, so a model that has learned one runtime's replies reads the
  // other's the same way. See packages/server-python/.../server.py.

  private async subscribeEvents(nodeId: string, severityMin: number, requested: number) {
    // Clamped, and reported as clamped: the buffer is memory this process holds
    // for as long as the subscription lives, and "as many as you like" was a
    // request with no ceiling at all (issue #139).
    const bufferSize = eventBufferSize(requested);
    let replaced: boolean;
    try {
      ({ replaced } = await this.events.subscribe(
        this.requireSession(),
        nodeId,
        severityMin,
        bufferSize
      ));
    } catch (error) {
      throw new Error(
        message("eventSubscribeFailed", { node_id: nodeId, reason: describeError(error) })
      );
    }

    return objectResult({
      node_id: canonicalNodeId(nodeId),
      severity_min: severityMin,
      buffer_size: bufferSize,
      replaced,
    });
  }

  private readEvents(nodeId: string, limit: number) {
    const drained = this.events.drain(this.requireSession(), nodeId, limit);
    if (drained === null) {
      throw new Error(message("notSubscribedToEvents", { node_id: nodeId }));
    }
    // In the response, not only on stderr: an agent that cannot tell a complete
    // event stream from one that lost alarms reads the gap as quiet. As a field
    // since issue #137, and as a sentence still for a reader of the text alone.
    const result = withNotice(
      eventResult(
        drained.records,
        drainCompleteness({
          returned: drained.records.length,
          limit,
          remaining: drained.remaining,
          dropped: drained.dropped,
        })
      ),
      drained.dropped > 0 ? droppedEventsMessage(drained.dropped, drained.size) : null
    );
    // The same reasoning for the gap a reconnect leaves: nothing was dropped
    // from the buffer, the events simply never arrived (#157).
    return withNotice(result, drained.resubscribed ? notice("eventsResubscribed") : null);
  }

  /** `read_event_history`: the events the server kept, for a range already past.
   *
   * `subscribe_events` only sees what arrives after it subscribes, so it cannot
   * answer what fired before anyone was watching. This reads the server's own
   * event archive instead, and returns the same records, so an alarm looks
   * identical whether it was seen live or recovered afterwards.
   */
  private async readEventHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    severityMin: number;
  }) {
    const { nodeId } = request;
    const end = toDate(request.end) ?? new Date();
    // An hour back, rather than the epoch: a range nobody bounded should be the
    // recent past, not the whole archive. `read_opcua_history` defaults the same
    // way and for the same reason.
    const start = toDate(request.start) ?? new Date(end.getTime() - 60 * 60 * 1000);
    // The same cap as a raw value read, and a refusal rather than a knob: an
    // alarm burst is tens of thousands of events, and "all of them" is a request
    // that never returns.
    const wanted = historyValues(request.numValues);

    try {
      const page = await readEventHistory(
        this.requireSession(),
        nodeId,
        start,
        end,
        wanted,
        request.severityMin
      );
      return historyResult(
        page.records,
        historyCompleteness({
          returned: page.records.length,
          fetched: page.fetched,
          wanted,
          continuationPoint: page.continued,
          nextStart: forwardFrom(start, end, page.lastTime),
        }),
        "eventHistoryTruncated"
      );
    } catch (error) {
      throw new Error(
        message("eventHistoryFailed", { node_id: nodeId, reason: describeError(error) })
      );
    }
  }

  private async listActiveAlarms(nodeId: string, timeoutSeconds: number) {
    let alarms: EventRecord[];
    try {
      alarms = await listActiveAlarms(this.requireSession(), nodeId, timeoutSeconds);
    } catch (error) {
      throw new Error(message("alarmsFailed", { node_id: nodeId, reason: describeError(error) }));
    }

    this.events.remember(alarms);
    return eventResult(alarms);
  }

  /** Both alarm tools, through one implementation.
   *
   * `acknowledge_alarm` is `act_on_alarm` with the action fixed, so there is only
   * ever one copy of "find the condition, resolve the method, call it, report the
   * status" to drift — the property the 17→13 consolidation was about, held to
   * here rather than assumed.
   *
   * The refusal wording is the one difference: `acknowledge_alarm` has said
   * `acknowledgeFailed` since it existed, and changing that would break a caller
   * matching on it for no gain.
   */
  private async actOnAlarm(
    eventId: string,
    action: string,
    comment: string,
    durationMs: number | null,
    conditionId?: string
  ) {
    // A relationship between two arguments, which the contract's own schema
    // cannot express: `shelveFor` is `shelve` plus a duration, and accepting one
    // on any other action would silently ignore it. Refusing says which action
    // the caller probably meant.
    if (action === "shelveFor" && durationMs === null) {
      throw new Error(message("shelveForNeedsDuration"));
    }
    if (action !== "shelveFor" && durationMs !== null) {
      throw new Error(message("shelveDurationNotAllowed", { action }));
    }

    const condition = conditionId || this.events.conditionFor(eventId);
    if (!condition) {
      throw new Error(message("unknownEventId", { event_id: eventId }));
    }

    const failed = (reason: string) =>
      new Error(
        action === "acknowledge"
          ? message("acknowledgeFailed", { condition_id: condition, reason })
          : message("alarmActionFailed", { action, condition_id: condition, reason })
      );

    let statusCode;
    try {
      statusCode = await alarmAction(
        this.requireSession(),
        condition,
        eventId,
        action,
        comment,
        durationMs
      );
    } catch (error) {
      throw failed(describeError(error));
    }
    // Good severity: an acknowledgement the server answered with a Good subcode
    // happened, and reporting it as a failure invites a retry. The subcode is in
    // `status`.
    if (!isGood(statusCode)) {
      throw failed(statusCode.name);
    }

    const record: Record<string, unknown> = {
      event_id: eventId,
      condition_id: canonicalNodeId(condition),
      status: statusCode.name,
    };
    // `acknowledge_alarm` answers with the shape it always has; `act_on_alarm`
    // adds the action, because 'shelve' and 'shelveFor' are one argument apart
    // and the record should say which one happened.
    if (action !== "acknowledge") {
      return objectResult({ ...record, action, status: statusCode.name });
    }
    return objectResult(record);
  }
}
