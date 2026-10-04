// The OPC UA tool implementations.
//
// Current-value reads delegate to the application port and native adapter.
// The remaining feature slices and execution pipeline move incrementally
// under #141; see docs/feature-modules.md.
import { invokeTool } from "./application/invocation.js";
import { executeTool, type ExecutionCall } from "./application/execution.js";
import type { ClientSession } from "node-opcua-client";
import { Resource, Tool } from "@modelcontextprotocol/sdk/types.js";

import {
  subscribeNodes,
  unsubscribeNodes,
  listSubscriptions,
  type SubscriptionRecord,
} from "./application/subscriptions.js";
import { NodeOpcuaSubscriptionPort } from "./adapters/opcua-subscriptions.js";
import { actOnAlarm, listAlarms } from "./application/alarms.js";
import { NodeOpcuaAlarmPort } from "./adapters/opcua-alarms.js";
import { subscribeEvents, readEvents, readEventHistory } from "./application/events.js";
import { NodeOpcuaEventPort } from "./adapters/opcua-events.js";
import { readHistory } from "./application/history.js";
import { NodeOpcuaHistoryPort } from "./adapters/opcua-history.js";
import { writeNodes, type WriteRequest } from "./application/write.js";
import { NodeOpcuaWritePort } from "./adapters/opcua-write.js";
import { callMethod } from "./application/methods.js";
import { NodeOpcuaMethodPort } from "./adapters/opcua-methods.js";
import { browseNodes, type BrowseRequest } from "./application/browse.js";
import { NodeOpcuaBrowsePort } from "./adapters/opcua-browse.js";
import { readNodes } from "./application/read.js";
import { NodeOpcuaReadPort } from "./adapters/opcua-read.js";
export { toNodeValueRecord } from "./adapters/opcua-read.js";

import { browseAllReferences } from "./browse.js";
import {
  type CapabilityAnswers,
  type CapabilityStatusRecord,
  answersFrom,
  capabilityStatus,
  refusal,
  requirements,
  unasked,
  verdict,
} from "./capabilities.js";
import { WARM_UP_WAIT_MS } from "./config.js";
import { OpcuaConnection, isConnectionError, notConnectedMessage } from "./connection.js";
import type { ToolName } from "./generated/contract-types.js";
import { CONTRACT, type ToolSpec } from "./contract.js";
import { NodeMetadata } from "./node-metadata.js";
import { AuditSink, AuditWriteError, buildRecord, operatorId } from "./audit.js";
import {
  ContractRefusal,
  ToolFailure,
  UnexpectedToolFailure,
  describeError,
  message,
} from "./errors.js";
import { eventBufferSize, historyValues } from "./limits.js";
import { Completeness, bufferCompleteness } from "./completeness.js";
import {
  ServerOperationLimits,
  UNSTATED,
  readChunk,
  readOperationLimits,
  writeLimit,
} from "./operation-limits.js";
import { notice } from "./notices.js";
import type { ServerStatusRecord } from "./application/diagnostics.js";
import { getServerStatus } from "./application/diagnostics.js";
import { NodeOpcuaDiagnosticsPort } from "./adapters/opcua-diagnostics.js";
import { DEFAULT_NOTIFIER, EVENT_DEFAULTS, EventSubscriptions } from "./events.js";
import { canonicalNodeId } from "./node-ids.js";
import { prettyJson } from "./result-text.js";
import { describeSecurity, securityConfig } from "./security.js";
import {
  SubscribeOptions,
  SubscriptionFilter,
  SubscriptionManager,
  resolveFilter,
} from "./subscriptions.js";
import {
  ToolPolicy,
  controlGate,
  pairsAt,
  serverIdentityRecord,
  toolPolicy,
  valuesAt,
} from "./policy.js";
import { randomBytes } from "crypto";

/** The standard Root and Objects folders, which a browse path is written from. */

export { checkEuRange } from "./application/write.js";
export { checkMaxChange } from "./adapters/opcua-write.js";

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
function eventResult(records: unknown[], completeness?: Completeness) {
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
    content: [{ type: "text", text: prettyJson(record) }],
    structuredContent: completeness ? { result: record, completeness } : { result: record },
  };
}

/** `resultShapes.serverStatus`: what the server says, and what it was found to support. */
type ServerStatusReport = ServerStatusRecord & { capabilities: CapabilityStatusRecord };

/** The diagnostics report (resultShapes.serverStatus). */
function statusResult(status: ServerStatusReport) {
  return objectResult(status);
}

function recordBlocks(records: unknown[], completeness?: Completeness) {
  return {
    content: records.map((record) => ({
      type: "text",
      text: prettyJson(record),
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
  /** What the server was found to support, and on which session generation.
   *  Never trusted across a generation — a restarted server may answer
   *  differently — and never consulted by `listTools` (#140). */
  private capabilities: CapabilityAnswers = unasked();
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
      // What the old session was told about the server's limits was true of it.
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
      // would re-enter the connect path it is standing in. Read now so the
      // operation limits and `get_server_status` have answers for the new
      // session; a call re-reads anything from an older generation itself.
      await this.probeCapabilities(session).catch(() => undefined);
    };
    // node-opcua re-created the session under us, typically because the server
    // restarted. The subscriptions came across with it; what this server learned
    // about the old session did not.
    this.conn.onSessionRestored = async (session) => {
      this.serverLimits = UNSTATED;
      this.metadata.serverLimits = UNSTATED;
      this.metadata.forget();
      await this.probeCapabilities(session).catch(() => undefined);
    };
  }

  /** Open the first connection and probe it. Started by `startWarmUp`.
   *
   * The Python runtime does this in its lifespan. The catalogue no longer
   * depends on it (#140) — what it buys is a first `get_server_status` that is
   * already connected, and capability answers already read, against a plant
   * that is up.
   *
   * Best-effort and never fatal: an MCP client starts this server when *it*
   * starts, which may be long before the plant network is reachable.
   * `get_server_status` reports what is wrong in the meantime, and every tool
   * call retries.
   */
  async warmUp(): Promise<void> {
    await this.conn.ensureConnection().catch(() => undefined);
    if (this.capabilities.generation !== this.conn.sessionGeneration) {
      await this.probeCapabilities().catch(() => undefined);
    }
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
   * What awaiting it bought is kept, within a bound. A status read during the
   * warm-up said "not connected" against a plant that was up, so
   * `get_server_status` waits for it — see `awaitWarmUp` — and a tool call that
   * needs a session joins its round through `ensureConnection`, as it always
   * did. `tools/list` does not wait: since #140 there is nothing in the
   * catalogue for the warm-up to change.
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
   * For `get_server_status`. Against a plant that is up the warm-up finishes
   * well inside the window, so the first status is a connected one. Against a
   * plant that is down it can take the whole round, and a server that is to be
   * diagnosable has to answer before then — from what it knows. The Python
   * server's `ServerState.await_warm_up` is the same wait.
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

  /** Read what the connected OPC UA server can do, off the session we already have.
   *
   * Never connects: `on`, or the session held, or nothing. The answers are
   * stamped with the generation of the session they were read on, captured
   * before asking — so an answer that arrives after the session has been
   * replaced is already stale when it is stored, and the next call asks again.
   */
  private async probeCapabilities(on?: ClientSession): Promise<CapabilityAnswers> {
    const session = on ?? this.session;
    // Deliberately not cached. "No session yet" is not "this server supports
    // nothing", and caching it as though it were is what made a request served
    // during the warm-up poison the answer for the rest of the process.
    if (!session) return this.capabilities;
    const generation = this.conn.sessionGeneration;
    const history = await this.conn.accessHistoryDataCapability(session);
    const historyEvents = await this.conn.accessHistoryEventsCapability(session);
    // Read with the capabilities because it changes when they do — on a new
    // session — and a tool that needs it can then use it without a round trip.
    this.serverLimits = await readOperationLimits(session);
    this.metadata.serverLimits = this.serverLimits;
    const aggregate = await this.conn.serverCapabilitiesAggregateFunctions(session);
    this.capabilities = answersFrom(
      generation,
      new Date().toISOString(),
      { history, historyEvents, aggregate },
      aggregate.functions
    );
    return this.capabilities;
  }

  /** The server's stated operation limits, probing once if need be.
   *
   * Every tool that sends a batch asks here rather than reading the field, for
   * the reason `ensureCapabilities` asks again: a process that started while
   * the plant was down, or whose session has since been replaced, has not asked
   * *this* session yet, and "not asked" must not be read as "no limit".
   */
  private async operationLimits(): Promise<ServerOperationLimits> {
    if (this.capabilities.generation !== this.conn.sessionGeneration) {
      await this.probeCapabilities();
    }
    return this.serverLimits;
  }

  /** Ask the live server again, rebuilding the session first if it has died.
   *
   * What a refusal is decided on. A refusal taken from the cache touches
   * nothing, so it could never notice that the session behind it had gone and
   * the server come back with the feature. A probe that lost its session is
   * therefore followed by the same rebuild a tool call would do, and the probes
   * asked once more on the new session. The Python server's
   * `_fresh_capabilities` does the same through `OpcuaConnection.run`.
   */
  private async freshCapabilities(): Promise<CapabilityAnswers> {
    const session = this.conn.sessionId;
    const answers = await this.probeCapabilities();
    if (!answers.connectionLost) return answers;
    try {
      await this.conn.reconnect(session);
    } catch (error) {
      throw new ContractRefusal(notConnectedMessage(this.conn.endpointUrl, describeError(error)));
    }
    return await this.probeCapabilities();
  }

  /** Refuse a call the connected server cannot serve, before anything is sent.
   *
   * Called with a live session in hand, so it can ask rather than guess. A
   * cached "yes" is trusted for the session generation it was read on: if it
   * has gone stale, the server's own refusal of the request says so. Nothing
   * else is taken from the cache. An answer from an older generation, an
   * `unknown`, and above all a "no" are asked again first — a "no" refuses
   * without touching the network, so taken from the cache it could never find
   * out that the server had come back with the feature. What is left is the
   * server's own answer, and a refusal worded from the contract that says which
   * capability, which session, and what to do instead (#140). The Python
   * server's `_ensure_capabilities` decides the same way.
   */
  private async ensureCapabilities(spec: ToolSpec, args: Record<string, unknown>): Promise<void> {
    const groups = requirements(spec, args);
    if (groups.length === 0) return;
    let answers = this.capabilities;
    let decided = verdict(groups, answers.support);
    if (answers.generation !== this.conn.sessionGeneration || decided.outcome !== "allowed") {
      answers = await this.freshCapabilities();
      decided = verdict(groups, answers.support);
    }
    if (decided.outcome === "allowed") return;
    throw new ContractRefusal(refusal(spec.name, decided, answers, this.conn.endpointUrl));
  }

  /** The advertised tool list: the contract, filtered by deployment policy only.
   *
   * No network I/O, no waiting, and the same answer for the life of the process
   * whatever the plant is doing (#140). It used to depend on the capabilities
   * of the session held — `read_event_history` absent, `aggregate_function`
   * withheld — so a process started while the plant was down advertised less
   * than one started while it was up, and nothing portable told a client that
   * had listed once to list again. Clients and models cache tool definitions
   * for the life of a session; what they cache now stays true. A capability the
   * server lacks is reported by the call that needs it (`ensureCapabilities`)
   * and by `get_server_status`.
   *
   * The policy is configuration, fixed when the process starts, so filtering
   * on it does not make the catalogue move. No `notifications/tools/list_changed`
   * is sent, here or on the Python runtime — see docs/architecture.md — and
   * nothing depends on one.
   */
  async listTools(): Promise<Tool[]> {
    return this.policy.visibleTools(CONTRACT.tools).map((tool) => ({
      name: tool.name,
      description: tool.description,
      inputSchema: tool.inputSchema,
      annotations: tool.annotations,
      outputSchema: outputSchema(tool),
    })) satisfies Tool[];
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
          text: prettyJson({ [resource.body.recordsKey]: this.subs.list() }),
        },
      ],
    };
  }

  /** Convert one protocol request through the common execution envelope. */
  async callTool(request: { params: { name: string; arguments?: Record<string, unknown> } }) {
    const name = request.params.name,
      args = request.params.arguments ?? {};
    try {
      return await executeTool(
        {
          newCallId,
          waitForConnection: () => this.awaitConnectionInFlight(),
          authorize: (name, args) => this.policy.authorize(name, args),
          allowed: (call) =>
            auditPermission(
              this.audit,
              this.policy,
              this.conn,
              call.name,
              call.arguments,
              call.callId,
              call.attempt
            ),
          after: (call, decision, reason) =>
            auditAfter(
              this.audit,
              this.policy,
              this.conn,
              call.name,
              call.arguments,
              decision,
              call.callId,
              call.attempt,
              reason
            ),
          run: (call) => this.runTool(call),
          normalizeFailure: (_name, error) => error,
          normalizeResult: (result) => result,
        },
        name,
        args
      );
    } catch (error) {
      return { content: [{ type: "text", text: describeError(error) }], isError: true };
    }
  }

  private async runTool(call: ExecutionCall) {
    return await invokeTool(
      {
        endpoint: () => this.conn.endpointUrl,
        session: () => this.conn.sessionId,
        hasConnection: () => true,
        waitForWarmUp: () => this.awaitWarmUp(),
        connect: () => this.ensureConnection(),
        capabilities: (call) => this.ensureCapabilities(call.spec, call.arguments),
        dispatch: async (call) =>
          call.name === "get_server_status"
            ? await this.invokeFeature(call.spec.name, call.arguments, async () =>
                statusResult(await this.getServerStatus())
              )
            : await this.invokeFeature(call.spec.name, call.arguments),
        isConnectionError,
        reconnect: (session) => this.conn.reconnect(session),
        logRecovery: (resend) =>
          console.error(
            `OPC UA call failed on a dead session; reconnecting${resend ? " and retrying once" : ""}`
          ),
        targets: (call) => describeTargets(call.spec, call.arguments),
        authorize: (call) => this.policy.authorize(call.name, call.arguments),
        allowed: (call) =>
          auditPermission(
            this.audit,
            this.policy,
            this.conn,
            call.name,
            call.arguments,
            call.callId,
            call.attempt
          ),
        denied: (call, reason) =>
          auditAfter(
            this.audit,
            this.policy,
            this.conn,
            call.name,
            call.arguments,
            "denied",
            call.callId,
            call.attempt,
            reason
          ),
      },
      call
    );
  }

  /** Anticipated failures keep their wording; crashes share the Python SDK boundary. */
  private async invokeFeature(
    name: ToolName,
    args: Record<string, unknown>,
    operation = () => this.dispatch(name, args)
  ) {
    try {
      return await operation();
    } catch (error) {
      // Recovery still sees native connection failures, including their causes.
      if (error instanceof ToolFailure || isConnectionError(error)) throw error;
      throw new UnexpectedToolFailure(name, error);
    }
  }

  /** Run one tool. The caller has already authorized it and ensured a session. */
  private async dispatch(name: ToolName, args: Record<string, unknown>) {
    switch (name) {
      case "get_server_status":
        return statusResult(await this.getServerStatus());

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
        const listed = listSubscriptions(this.subscriptionPort());
        return recordBlocks(listed.records, listed.completeness);

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
          args.condition_id as string | undefined,
          true
        );

      case "act_on_alarm":
        return await this.actOnAlarm(
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

  /** The `get_server_status` report: connection state, then what the server says.
   *
   * Connecting is attempted rather than assumed, so asking for the status is
   * also the cheapest way to bring a dropped connection back. A failure to
   * connect is the answer, not an error — "not connected, and here is why" is
   * exactly what the caller asked for.
   *
   * `capabilities` is appended after the read, because the read may have
   * re-established the session and re-read them. It is the cache as it stands,
   * never a probe of its own: it says which session generation it was read on
   * and when, which is what makes a stale answer recognisable as one (#140).
   */
  private async getServerStatus(): Promise<ServerStatusReport> {
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

  private requireSession(): ClientSession {
    if (!this.session) {
      throw new ToolFailure("No OPC UA session available");
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
   *
   * More than `limits.maxNodesPerRead` is refused by the input schema's
   * `maxItems` before this runs: a short list of readings is indistinguishable
   * from a complete one, so the list is never quietly cut. A server whose
   * MaxNodesPerRead is lower gets the list in consecutive Reads instead.
   */
  private async readOpcuaNodes(nodeIds: string[]) {
    const port = new NodeOpcuaReadPort(this.requireSession(), this.metadata);
    return recordBlocks(await readNodes(port, nodeIds, readChunk(await this.operationLimits())));
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
    const port = new NodeOpcuaHistoryPort(this.requireSession());
    const result = await readHistory(port, request, this.capabilities.aggregateFunctions);
    return historyResult(result.records, result.completeness, "historyTruncated");
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
  private async browseOpcuaNodes(request: BrowseRequest) {
    const port = new NodeOpcuaBrowsePort(this.requireSession(), () => this.operationLimits());
    const result = await browseNodes(port, request);
    return objectResult(result.result, result.completeness);
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
    const port = new NodeOpcuaMethodPort(this.requireSession());
    return objectResult(await callMethod(port, objectNodeId, methodNodeId, methodArgs));
  }

  // --- data-change subscriptions -------------------------------------------

  private subscriptionPort() {
    return new NodeOpcuaSubscriptionPort(() => this.requireSession(), this.subs, this.metadata);
  }
  private async subscribeOpcuaNodes(
    nodeIds: string[],
    options: SubscribeOptions,
    filter: SubscriptionFilter
  ) {
    const result = await subscribeNodes(this.subscriptionPort(), nodeIds, options, filter);
    return recordBlocks(result.records, result.completeness);
  }
  private async unsubscribeOpcuaNodes(subscriptionIds: string[]) {
    const result = await unsubscribeNodes(this.subscriptionPort(), subscriptionIds);
    return recordBlocks(result.records, result.completeness);
  }

  // --- events and Alarms & Conditions ------------------------------------------
  // The wording of every message below is shared with the Python server's
  // `events` tools, so a model that has learned one runtime's replies reads the
  // other's the same way. See packages/server-python/.../server.py.

  private eventPort() {
    return new NodeOpcuaEventPort(this.requireSession(), this.events);
  }
  private async subscribeEvents(nodeId: string, severityMin: number, requested: number) {
    return objectResult(await subscribeEvents(this.eventPort(), nodeId, severityMin, requested));
  }
  private async readEvents(nodeId: string, limit: number) {
    const result = await readEvents(this.eventPort(), nodeId, limit);
    let response = eventResult(result.records, result.completeness);
    for (const text of result.notices) response = withNotice(response, text);
    return response;
  }
  private async readEventHistory(request: {
    nodeId: string;
    start?: string;
    end?: string;
    numValues: number;
    severityMin: number;
  }) {
    const result = await readEventHistory(this.eventPort(), request);
    return historyResult(result.records, result.completeness, "eventHistoryTruncated");
  }

  private alarmPort() {
    return new NodeOpcuaAlarmPort(() => this.requireSession(), this.events);
  }
  private async listActiveAlarms(nodeId: string, timeoutSeconds: number) {
    return eventResult(await listAlarms(this.alarmPort(), nodeId, timeoutSeconds));
  }
  private async actOnAlarm(
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
}
