/** Instance-owned native connection, generation caches and subscription lifecycle. */
import type { ClientSession } from "node-opcua-client";
import { OpcuaConnection, notConnectedMessage } from "../connection.js";
import { SubscriptionManager } from "../subscriptions.js";
import { EventSubscriptions } from "../events.js";
import { NodeMetadata } from "../node-metadata.js";
import { WARM_UP_WAIT_MS } from "../config.js";
import {
  type CapabilityAnswers,
  answersFrom,
  refusal,
  requirements,
  unasked,
  verdict,
} from "../capabilities.js";
import { type ServerOperationLimits, UNSTATED, readOperationLimits } from "../operation-limits.js";
import type { ToolSpec } from "../contract.js";
import { ContractRefusal, describeError } from "../errors.js";
export class NodeOpcuaRuntime {
  capabilities: CapabilityAnswers = unasked();

  readonly subs = new SubscriptionManager();

  readonly events = new EventSubscriptions();

  readonly metadata = new NodeMetadata();

  warmUpPromise: Promise<void> | null = null;

  warmUpDeadline = 0;

  warmUpWaitMs = WARM_UP_WAIT_MS;

  serverLimits: ServerOperationLimits = UNSTATED;

  async warmUp(): Promise<void> {
    await this.conn.ensureConnection().catch(() => undefined);
    if (this.capabilities.generation !== this.conn.sessionGeneration) {
      await this.probeCapabilities().catch(() => undefined);
    }
  }

  startWarmUp(warmUp: () => Promise<void> = () => this.warmUp()): Promise<void> {
    if (!this.warmUpPromise) {
      this.warmUpDeadline = Date.now() + this.warmUpWaitMs;
      this.warmUpPromise = warmUp();
    }
    return this.warmUpPromise;
  }

  async awaitConnectionInFlight(): Promise<void> {
    if (this.warmUpPromise) await this.warmUpPromise;
    await this.conn.settled();
  }

  async awaitWarmUp(): Promise<void> {
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

  async shutdown(): Promise<void> {
    await this.subs.closeAll();
    await this.events.closeAll();
  }

  get session(): ClientSession | null {
    return this.conn.session;
  }

  ensureConnection(): Promise<void> {
    return this.conn.ensureConnection();
  }

  async probeCapabilities(on?: ClientSession): Promise<CapabilityAnswers> {
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

  async operationLimits(): Promise<ServerOperationLimits> {
    if (this.capabilities.generation !== this.conn.sessionGeneration) {
      await this.probeCapabilities();
    }
    return this.serverLimits;
  }

  async freshCapabilities(): Promise<CapabilityAnswers> {
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

  async ensureCapabilities(spec: ToolSpec, args: Record<string, unknown>): Promise<void> {
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

  requireSession(): ClientSession {
    if (!this.session) {
      throw new ContractRefusal("No OPC UA session available");
    }
    return this.session;
  }

  constructor(readonly conn: OpcuaConnection) {
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
}
