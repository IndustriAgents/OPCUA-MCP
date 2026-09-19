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
  StatusCodes,
  CallMethodResult,
  NodeClass,
  HistoryData,
  AggregateFunction,
  ClientSession,
} from "node-opcua-client";
import { Resource, Tool } from "@modelcontextprotocol/sdk/types.js";

import { browseAllReferences } from "./browse.js";
import { OpcuaConnection, isConnectionError, notConnectedMessage } from "./connection.js";
import { CONTRACT, type ToolSpec } from "./contract.js";
import { NodeMetadata, withinRange, type AnalogInfo } from "./node-metadata.js";
import { AuditSink, operatorId } from "./audit.js";
import { ContractRefusal, message } from "./errors.js";
import {
  MAX_NODES_PER_READ,
  MAX_SUBSCRIPTIONS,
  historyValues,
  historyWasClipped,
} from "./limits.js";
import { notice } from "./notices.js";
import { validateArguments } from "./validation.js";
import { ServerStatusRecord, disconnectedStatus, readServerStatus } from "./diagnostics.js";
import { toDate } from "./dates.js";
import {
  DEFAULT_NOTIFIER,
  EVENT_DEFAULTS,
  EventRecord,
  EventSubscriptions,
  acknowledgeAlarm,
  droppedEventsMessage,
  listActiveAlarms,
} from "./events.js";
import { canonicalNodeId } from "./node-ids.js";
import { toHistoryRecords, toIsoUtc, variantToJson } from "./records.js";
import { describeSecurity, securityConfig } from "./security.js";
import {
  SubscribeOptions,
  SubscriptionManager,
  SubscriptionRecord,
  unknownSubscriptionsMessage,
} from "./subscriptions.js";
import {
  ToolPolicy,
  asNumber,
  formatNumber,
  pairsAt,
  toolPolicy,
  valuesAt,
  type ValueBound,
} from "./policy.js";
import { convertForVariant } from "./variant-codec.js";
import { randomBytes } from "crypto";

/** The standard Root and Objects folders, which a browse path is written from. */
const ROOT_FOLDER = "ns=0;i=84";

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

/** The pre-#10 argument heuristic, kept only for methods that declare no types.
 *
 * Parses float → int → string and forces Double or String. It is wrong for
 * Boolean and every sized integer, which is what `inputArgumentTypes` exists to
 * fix; this remains because a method that publishes no InputArguments leaves
 * nothing better to go on.
 */
function guessVariant(arg: unknown): Variant {
  if (typeof arg === "boolean") return new Variant({ dataType: DataType.Boolean, value: arg });
  if (typeof arg === "number") return new Variant({ dataType: DataType.Double, value: arg });
  const text = String(arg);
  const asNumber = Number(text);
  return Number.isFinite(asNumber) && text.trim() !== ""
    ? new Variant({ dataType: DataType.Double, value: asNumber })
    : new Variant({ dataType: DataType.String, value: text });
}

/** A node's present reading as a number, or null if there is not one to compare. */
function currentNumber(dataValue: DataValue | undefined): number | null {
  if (!dataValue || dataValue.statusCode !== StatusCodes.Good) return null;
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
      : dataValue.statusCode !== StatusCodes.Good
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
function toNodeValueRecord(
  nodeId: string,
  dataValue: DataValue | undefined,
  engineering: AnalogInfo | null = null
): NodeValueRecord {
  const good = dataValue?.statusCode === StatusCodes.Good;
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

/** A history/aggregate response: one text block per canonical record.
 *
 * The framing is part of the contract (resultShapes.historyRecords), not an
 * implementation detail: FastMCP splits the Python server's returned list into
 * one block per element, so the Node server does the same rather than emitting a
 * single array — the two servers' responses are then read the same way.
 */
/** History records, with a notice when the call hit the per-call maximum.
 *
 * A trailing plain-text block rather than a field, because `historyRecords` is
 * an array of readings and a truncation flag is not a reading — the same shape
 * and the same reason `read_events` reports dropped events this way. Outside
 * `structuredContent` for the same reason.
 *
 * Only when the cap itself was reached: a caller who asked for 10 and got 10 has
 * what they asked for.
 */
function historyResult(dataValues: DataValue[] | null | undefined, wanted?: number) {
  const records = toHistoryRecords(dataValues);
  const result = recordBlocks(records);
  if (wanted === undefined || !historyWasClipped(records.length, wanted)) return result;
  return {
    ...result,
    content: [
      ...result.content,
      { type: "text", text: notice("historyTruncated", { count: records.length }) },
    ],
  };
}

/** The same framing for the subscription family (resultShapes.subscriptionRecords). */
function subscriptionResult(records: SubscriptionRecord[]) {
  return recordBlocks(records);
}

/** And for the event family (resultShapes.eventRecords). */
function eventResult(records: EventRecord[]) {
  return recordBlocks(records);
}

/** A result that is one object rather than a list of records.
 *
 * One text block and a `result` that is the object itself. Used by every shape
 * where a list would be a lie about the answer's structure: a browse has one
 * `truncated` flag for the whole walk, a method call has one result, and a
 * status report is one report. The Python server frames these identically.
 */
function objectResult(record: unknown) {
  return {
    content: [{ type: "text", text: JSON.stringify(record, null, 2) }],
    structuredContent: { result: record },
  };
}

/** The diagnostics report (resultShapes.serverStatus). */
function statusResult(status: ServerStatusRecord) {
  return objectResult(status);
}

function recordBlocks(records: unknown[]) {
  return {
    content: records.map((record) => ({
      type: "text",
      text: JSON.stringify(record, null, 2),
    })),
    structuredContent: { result: records },
  };
}

function outputSchema(resultShape: string | undefined): Tool["outputSchema"] {
  if (!resultShape) return undefined;
  return {
    type: "object",
    properties: { result: CONTRACT.resultShapes[resultShape] },
    required: ["result"],
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

/** Write one line of the control audit trail to stderr.
 *
 * Only `control` and `alarm-action` tools: an audit trail that also recorded
 * every read would bury the four lines anyone is looking for.
 *
 * Never the *values* being written, only the targets. A setpoint is process
 * data, and this stream is the one an MCP client shows the user and a log
 * collector ships off the machine.
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
  sink.write({
    event: "opcua_mcp_policy",
    timestamp: new Date().toISOString(),
    // Second, so it is next to the timestamp in the line an operator reads and
    // can be grepped for to pull one call's whole story out of a shipped log.
    call_id: callId,
    // Which physical attempt this line is about. One call can reach the plant
    // twice — the session dies, the connection is rebuilt, the request is
    // re-sent — and a trail whose purpose is "what reached the plant" has to
    // count those separately rather than fold them into one line.
    attempt,
    // Which plant, and which of this process's sessions. A node id is not stable
    // across a server restart — that is the whole reason the `nsu=` allowlist
    // form exists — so "a write to ns=2;i=5 was allowed" is only interpretable
    // later alongside where it went and over which session.
    endpoint: connection.endpointUrl,
    session: connection.sessionId,
    // On whose behalf, as the deployment chose to record it. null when
    // OPCUA_OPERATOR_ID is unset, which is honest: this server has no notion of
    // who is calling, and a name nothing verified would be worse than none.
    operator: operatorId(),
    profile: policy.config.profile,
    tool: name,
    decision,
    ...auditTargets(tool, args),
    ...(reason ? { reason } : {}),
  });
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
      // A new session may be a restarted server, whose nodes are not necessarily
      // the nodes the old ids named. What each one said about its unit and its
      // range was true of the session that said it.
      this.metadata.forget();
      await this.subs.reattach(session);
      // With the session in hand, not through the connection: this runs inside
      // `reconnect()`, and a probe that called `ensureConnection()` from here
      // would re-enter the connect path it is standing in.
      await this.probeCapabilities(session).catch(() => undefined);
    };
  }

  /** Open the first connection and probe it, before any request is served.
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
    this.aggregateFunctions = await this.serverCapabilitiesAggregateFunctions(session);

    // A tool gated on capabilities is offered when the server reports *any* of
    // them. `read_opcua_history` lists both: a server with only aggregates can
    // still answer an aggregate read, and gating it on `history` alone would
    // hide the one thing such a server is good at.
    if (historyOk) available.add("history");
    if (this.aggregateFunctions.length > 0) available.add("aggregate");
    this.capabilities = available;
    return available;
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
        outputSchema: outputSchema(tool.resultShape),
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
      validateArguments(name, spec.inputSchema, args);

      // This is the security boundary. Filtering tools/list improves the model's
      // choices, but clients cache catalogs and may call a previously visible
      // tool directly, so authorize again before touching the OPC UA network.
      try {
        this.policy.authorize(name, args);
        authorized = true;
        auditDecision(this.audit, this.policy, this.conn, name, args, "allowed", callId, 1);
      } catch (error) {
        auditDecision(
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
      // The one tool that must answer while the connection is down: it exists to
      // say so. Everything below needs a session first.
      if (name === "get_server_status") {
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
      auditDecision(
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
        auditDecision(
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
      auditDecision(
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
    auditDecision(this.audit, this.policy, this.conn, spec.name, args, "allowed", callId, 2);

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

      case "write_opcua_nodes":
        return await this.writeOpcuaNodes(args.nodes as WriteRequest[]);

      case "call_opcua_method":
        return await this.callOpcuaMethod(
          args.object_node_id as string,
          args.method_node_id as string,
          args.arguments as unknown[] | undefined
        );

      case "subscribe_opcua_nodes":
        return await this.subscribeOpcuaNodes(args.node_ids as string[], {
          publishingInterval: args.publishing_interval as number | undefined,
          samplingInterval: args.sampling_interval as number | undefined,
          bufferSize: args.buffer_size as number | undefined,
        });

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
        return await this.acknowledgeAlarm(
          args.event_id as string,
          (args.comment as string) ?? "",
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
    try {
      // Through the same retry as every other read, so that asking for the
      // status also re-establishes a session that has silently died — which is
      // exactly the moment someone asks.
      return await this.conn.withRetry(() =>
        readServerStatus(this.requireSession(), endpoint, security)
      );
    } catch (error) {
      return disconnectedStatus(endpoint, security, describeError(error));
    }
  }

  private requireSession(): ClientSession {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }
    return this.session;
  }

  // --- reading -------------------------------------------------------------

  /** `read_opcua_nodes`: the current value of one or more nodes, fully qualified.
   *
   * One `read` for the whole list, so fifty nodes cost one round trip. A node
   * the server rejects is one record with a `Bad…` status among the others —
   * promoting it to an error would discard every other node's value, which is
   * the opposite of what asking for them together is for.
   */
  private async readOpcuaNodes(nodeIds: string[]) {
    const session = this.requireSession();
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error(message("emptyArray", { tool: "read_opcua_nodes", argument: "node_ids" }));
    }
    // Refused, not truncated: a short list of readings is indistinguishable from
    // a complete one, and dropping nodes from a read is the kind of quiet wrong
    // answer the browse caps exist to prevent.
    if (nodeIds.length > MAX_NODES_PER_READ) {
      throw new Error(
        message("tooManyNodes", {
          tool: "read_opcua_nodes",
          limit: MAX_NODES_PER_READ,
          count: nodeIds.length,
        })
      );
    }

    try {
      const dataValues = await session.read(
        nodeIds.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value }))
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
        const historyReadings = await session.readHistoryValue(
          [nodeId],
          toDate(request.start) as any,
          toDate(request.end) as any,
          { numValuesPerNode: wanted }
        );
        if (historyReadings.length !== 1) throw new Error("Read history failed");
        if (historyReadings[0].statusCode !== StatusCodes.Good) {
          throw new Error(`Read history failed with status: ${historyReadings[0].statusCode.name}`);
        }
        return historyResult((historyReadings[0].historyData as HistoryData).dataValues, wanted);
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

      const aggregated = await session.readAggregateValue(
        { nodeId },
        toDate(request.start) as any,
        (toDate(request.end) ?? new Date()) as any,
        AggregateFunction[aggregateFunction as keyof typeof AggregateFunction],
        request.processingInterval
      );
      if (aggregated.statusCode !== StatusCodes.Good) {
        throw new Error(`Read aggregate failed with status: ${aggregated.statusCode.name}`);
      }
      // No cap on an aggregate read: the number of results is decided by
      // `processing_interval` over the range, which is the whole point of asking
      // for one — it is how to see a week without transferring a week.
      return historyResult((aggregated.historyData as HistoryData).dataValues);
    } catch (error) {
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
            if (current.nodeId === root) throw error;
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

      if (includeValues) await this.fillVariableDetail(session, found);
      return objectResult({ nodes: found, truncated, inspected });
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
    if (browseName.statusCode !== StatusCodes.Good) {
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
    };
  }

  /** Fill in value, data type and description for the Variables among `records`.
   *
   * One `read` for everything rather than three per node: a 500-node inventory
   * is otherwise 1500 round trips, which is the difference between a tool that
   * answers and one that times out on real equipment.
   */
  private async fillVariableDetail(
    session: ClientSession,
    records: NodeRefRecord[]
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
      values = await session.read(reads);
    } catch {
      // Best-effort enrichment: the nodes were found, and reporting them
      // without their values beats failing a browse that succeeded.
      return;
    }

    variables.forEach((record, index) => {
      const [value, dataType, description] = values.slice(index * 3, index * 3 + 3);
      if (value?.statusCode === StatusCodes.Good) {
        record.value = variantToJson(value.value);
        record.data_type = dataTypeName(value.value);
      }
      if (record.data_type === null && dataType?.statusCode === StatusCodes.Good) {
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
   */
  private async writeOpcuaNodes(nodes: WriteRequest[]) {
    const session = this.requireSession();
    if (!Array.isArray(nodes) || nodes.length === 0) {
      throw new Error(message("emptyArray", { tool: "write_opcua_nodes", argument: "nodes" }));
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
          ? await session.read(
              needsCurrent.map((index) => ({
                nodeId: nodes[index].node_id,
                attributeId: AttributeIds.Value,
              }))
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
            if (!dataValue || dataValue.statusCode !== StatusCodes.Good || !dataValue.value) {
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
        if (declaredType === undefined) return guessVariant(arg);
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
      if (callResult.statusCode !== StatusCodes.Good) {
        throw new Error(`Method call failed with status: ${callResult.statusCode.name}`);
      }

      return objectResult({
        object_node_id: canonicalNodeId(objectNodeId),
        method_node_id: canonicalNodeId(methodNodeId),
        status: callResult.statusCode.name,
        outputs: (callResult.outputArguments ?? []).map((variant) => variantToJson(variant)),
      });
    } catch (error) {
      throw new Error(
        message("methodFailed", {
          method_node_id: methodNodeId,
          object_node_id: objectNodeId,
          reason: describeError(error),
        })
      );
    }
  }

  /** The declared type of each input argument, or [] when the method publishes none. */
  private async inputArgumentTypes(
    session: ClientSession,
    methodNodeId: string
  ): Promise<Array<{ dataType: DataType; arrayType: VariantArrayType }>> {
    try {
      const definition = await session.getArgumentDefinition(methodNodeId);
      return (definition.inputArguments ?? []).map((argument) => ({
        // Built-in types are numbered identically in the DataType enum and in
        // namespace 0, which is what makes this a lookup rather than a table.
        dataType: Number(argument.dataType.value) as DataType,
        arrayType: argument.valueRank >= 1 ? VariantArrayType.Array : VariantArrayType.Scalar,
      }));
    } catch {
      // Not every method publishes InputArguments, and a method with no
      // arguments has nothing to publish. Fall back rather than refuse.
      return [];
    }
  }

  // --- data-change subscriptions -------------------------------------------

  private async subscribeOpcuaNodes(nodeIds: string[], options: SubscribeOptions) {
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
    const records: SubscriptionRecord[] = [];
    for (const nodeId of nodeIds) {
      // Named per node, as the Python runtime words it: a batch that fails on
      // its fourth node should say which one, not report the library's own
      // phrasing for whichever call happened to throw.
      try {
        records.push(await this.subs.subscribe(session, nodeId, options));
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

  private async subscribeEvents(nodeId: string, severityMin: number, bufferSize: number) {
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
    const result = eventResult(drained.records);
    if (drained.dropped > 0) {
      // In the response, not only on stderr: an agent that cannot tell a
      // complete event stream from one that lost alarms reads the gap as quiet.
      result.content.push({
        type: "text",
        text: droppedEventsMessage(drained.dropped, drained.size),
      });
    }
    return result;
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

  private async acknowledgeAlarm(eventId: string, comment: string, conditionId?: string) {
    const condition = conditionId || this.events.conditionFor(eventId);
    if (!condition) {
      throw new Error(message("unknownEventId", { event_id: eventId }));
    }

    let statusCode;
    try {
      statusCode = await acknowledgeAlarm(this.requireSession(), condition, eventId, comment);
    } catch (error) {
      throw new Error(
        message("acknowledgeFailed", { condition_id: condition, reason: describeError(error) })
      );
    }
    if (statusCode !== StatusCodes.Good) {
      throw new Error(
        message("acknowledgeFailed", { condition_id: condition, reason: statusCode.name })
      );
    }

    return objectResult({
      event_id: eventId,
      condition_id: canonicalNodeId(condition),
      status: statusCode.name,
    });
  }
}
