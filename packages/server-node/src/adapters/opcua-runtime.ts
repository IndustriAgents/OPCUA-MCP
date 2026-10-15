/** Instance-owned native connection, generation caches and subscription lifecycle. */
import type { ClientSession } from "node-opcua-client";
import { OpcuaConnection, notConnectedMessage } from "../connection.js";
import { SubscriptionManager } from "../subscriptions.js";
import { EventSubscriptions } from "../events.js";
import { NodeMetadata } from "../node-metadata.js";
import { NodeFactsCache } from "../node-facts.js";
import { checkPolicy, type PolicyCheckRecord } from "../policy-resolution.js";
import type { ToolPolicy } from "../policy.js";
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

  /** What each node says about itself — class, type, shape, access — for the life
   *  of one session; what the write and method plans are decided on. */
  readonly facts = new NodeFactsCache();

  /** The policy check on the current session, for `get_server_status`. */
  policyCheck: PolicyCheckRecord | null = null;

  /** Which session generation the policy was last resolved on. */
  private policyGeneration: number | null = null;

  /** The resolution in flight, which a call waits for before it is authorized:
   *  until it ends, browse-path entries match nothing and deny_read refuses. */
  private policyResolution: Promise<void> | null = null;

  warmUpPromise: Promise<void> | null = null;

  warmUpDeadline = 0;

  warmUpWaitMs = WARM_UP_WAIT_MS;

  serverLimits: ServerOperationLimits = UNSTATED;

  async warmUp(): Promise<void> {
    await this.conn.ensureConnection().catch(() => undefined);
    if (this.capabilities.generation !== this.conn.sessionGeneration) {
      await this.probeCapabilities().catch(() => undefined);
    }
    // The first session is not announced through `onSessionReplaced`, so its
    // policy is resolved here — before the first call is authorized, which waits
    // for the warm-up.
    await this.ensurePolicy();
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
    // And the policy resolution a new session started: browse-path entries and
    // deny_read are what `authorize` is about to read.
    if (this.policyResolution) await this.policyResolution;
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
    this.facts.serverLimits = this.serverLimits;
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

  constructor(
    readonly conn: OpcuaConnection,
    private readonly policy: ToolPolicy
  ) {
    // A rebuilt connection is a new session, and an OPC UA subscription belongs
    // to the session that created it. Without this, a server restart would leave
    // every `subscribe_opcua_nodes` handle the agent holds silently dead.
    this.conn.onSessionReplaced = async (session) => {
      this.forgetSession();
      await this.subs.reattach(session);
      await this.events.reattach(session);
      // With the session in hand, not through the connection: this runs inside
      // `reconnect()`, and a probe that called `ensureConnection()` from here
      // would re-enter the connect path it is standing in. Read now so the
      // operation limits and `get_server_status` have answers for the new
      // session; a call re-reads anything from an older generation itself.
      await this.learnSession(session);
    };
    // node-opcua re-created the session under us, typically because the server
    // restarted. The subscriptions came across with it; what this server learned
    // about the old session did not.
    this.conn.onSessionRestored = async (session) => {
      this.forgetSession();
      await this.learnSession(session);
    };
  }

  /** Drop everything learned about the session that has just gone.
   *
   * What the old session was told about the server's limits was true of it. A
   * new session may be a restarted server, whose nodes are not necessarily the
   * nodes the old ids named: what each said about its unit, its range, its type
   * and its access was true of the session that said it, and so was where every
   * browse path in the policy led. Until the policy is resolved again it allows
   * nothing by path and deny_read refuses guarded reads.
   */
  private forgetSession(): void {
    this.events.forgetConditions();
    this.serverLimits = UNSTATED;
    this.metadata.serverLimits = UNSTATED;
    this.metadata.forget();
    this.facts.serverLimits = UNSTATED;
    this.facts.forget();
    this.policyCheck = null;
    this.policyGeneration = null;
    this.policy.forgetResolution("it has not yet been resolved on this OPC UA session");
  }

  /** Probe a new session's capabilities, then resolve and check the policy on it.
   *
   * Tracked as the resolution in flight from its first line, so that a call
   * arriving meanwhile waits for it rather than meeting a policy that has been
   * emptied and not yet refilled.
   */
  private learnSession(session: ClientSession): Promise<void> {
    return this.track(
      (async () => {
        await this.probeCapabilities(session).catch(() => undefined);
        await this.resolvePolicy(session);
      })()
    );
  }

  private track(work: Promise<void>): Promise<void> {
    const tracked: Promise<void> = work.finally(() => {
      if (this.policyResolution === tracked) this.policyResolution = null;
    });
    this.policyResolution = tracked;
    return tracked;
  }

  /** Resolve the policy against `session` and keep the check (spec §6, §7).
   *
   * Never throws: a connect must not fail because the policy could not be
   * checked. What could not be resolved stays closed — `forgetResolution` has
   * already emptied it — and the next call on this generation tries again.
   */
  private async resolvePolicy(session: ClientSession): Promise<void> {
    const generation = this.conn.sessionGeneration;
    try {
      const record = await checkPolicy({
        session,
        policy: this.policy,
        facts: this.facts,
        metadata: this.metadata,
        serverLimits: this.serverLimits,
        generation,
      });
      // A session replaced while this ran has its own resolution coming.
      if (generation !== this.conn.sessionGeneration) return;
      this.policyCheck = record;
      this.policyGeneration = generation;
    } catch (error) {
      console.error(`WARNING: policy check could not complete: ${describeError(error)}`);
    }
  }

  /** The policy resolved on the session a call is about to use.
   *
   * A session opened by the call itself — the first connect, made after a start
   * with the plant down — has not been resolved yet, and neither has one whose
   * resolution failed; both are resolved here, once, before the strict read
   * check and before anything is sent.
   */
  async ensurePolicy(): Promise<void> {
    if (this.policyResolution) await this.policyResolution;
    const session = this.session;
    if (!session || this.policyGeneration === this.conn.sessionGeneration) return;
    await this.track(this.resolvePolicy(session));
  }
}
