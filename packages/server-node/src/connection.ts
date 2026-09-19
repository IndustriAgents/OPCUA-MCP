// Owns the OPC UA client/session lifecycle and the runtime capability probes.
//
// The connection is expected to break. A plant network drops, an OPC UA server
// is restarted for maintenance, a switch reboots — and an MCP server that needed
// restarting after any of those would be useless to leave running. So there are
// two layers of recovery here, and they do different jobs:
//
//   1. node-opcua repairs a broken channel itself, re-activating the *same*
//      session and re-creating its subscriptions. `connectionStrategy` (from
//      `config.ts`) decides how hard it tries. This is the cheap path: nothing
//      above this module notices.
//   2. When that gives up, the client is dead and stays dead. The next call to
//      `ensureConnection` therefore throws the old client away and builds a new
//      one — which is what lets a server that has been down for an hour be
//      picked up on the next tool call. A fresh session is a *different* session,
//      so `onSessionReplaced` lets the subscription manager re-establish what it
//      was monitoring.
//
// Capability probes are best-effort by design: an optional capability must
// never break tools/list, so a transient outage still leaves the core tools
// advertised.
// `node-opcua-client`, not the umbrella `node-opcua`: this is an OPC UA *client*,
// and the umbrella package's entry point drags in the server implementation
// alongside it. That half is not merely dead weight in the downloadable bundles
// — `node-opcua-server` and the address-space test helpers both read files
// relative to their own `__dirname` at *import* time to find their package.json,
// which no longer exists once bundled, so the `.mcpb` died on connect. Importing
// the client package keeps the compiler honest about what this server may use.
import { OPCUAClient, ClientSession, StatusCodes, AggregateFunction } from "node-opcua-client";

import { randomBytes } from "crypto";
import { setDefaultAutoSelectFamily } from "net";

import { SERVER_URL, reconnectBudgetMs, reconnectConfig } from "./config.js";
import { CONTRACT } from "./contract.js";
import { toolPolicy } from "./policy.js";
import {
  clientSecurityOptions,
  describeSecurity,
  securityConfig,
  securityWarnings,
  userIdentity,
} from "./security.js";
import { message } from "./errors.js";
import { transportSettings } from "./transport-limits.js";

// Happy Eyeballs: try IPv4 and IPv6 rather than only the first address DNS
// returns. Every Node this package now supports defaults to this, so the call is
// belt and braces — but an explicit `true` also survives a deployment that turns
// it off with `--no-network-family-autoselection`, under which the default
// `opc.tcp://localhost:4840` resolves to ::1 and fails outright against an OPC UA
// server listening on IPv4 rather than falling back to 127.0.0.1.
setDefaultAutoSelectFamily(true);

/** Where the connection is, as this module sees it.
 *
 * `reconnecting` is node-opcua's own repair in progress: the client object is
 * still usable and the session will come back on the same object, so the right
 * thing for a tool call to do is wait rather than build a second client.
 */
export type ConnectionState = "disconnected" | "connecting" | "connected" | "reconnecting";

/** How often `awaitReconnection` looks to see whether the repair has finished. */
const RECONNECT_POLL_MS = 100;

const DEAD_SESSION = CONTRACT.deadSession;

/** The OPC UA status codes that mean "the session is gone", by name.
 *
 * The contract names them and node-opcua supplies the numbers, so this list is
 * not a transcription of anything. A name the library stops publishing throws
 * here, at import, rather than quietly never matching again — which was the real
 * risk in the hand-written list this replaces, and the one its own test could not
 * see because it parametrised over the same constant.
 */
export const DEAD_SESSION_STATUS_CODES: Record<string, number> = Object.fromEntries(
  DEAD_SESSION.statusCodeNames.names.map((name) => {
    const code = (StatusCodes as unknown as Record<string, { value: number } | undefined>)[name];
    if (!code) {
      throw new Error(
        `contract deadSession.statusCodeNames names ${name}, which node-opcua does not publish`
      );
    }
    return [name, code.value];
  })
);

/** Failures below OPC UA, which have no status code to carry. Fixed by errno, so
 *  matching them in text is as stable as matching a code. */
export const SOCKET_ERROR_CODES: readonly string[] = DEAD_SESSION.socketErrors.codes;

/** The remainder, matched in the error's rendered text. The only fragile part of
 *  this, and deliberately the smallest: see the contract's own note. */
export const DEAD_SESSION_PHRASES: readonly string[] = DEAD_SESSION.phrases.texts;

/** Everything matched in text, in one list, because two of the three groups are
 *  and a caller asking "would this be retried" should not have to know which. */
export const DEAD_SESSION_MARKERS: readonly string[] = [
  ...Object.keys(DEAD_SESSION_STATUS_CODES),
  ...DEAD_SESSION_PHRASES,
  ...SOCKET_ERROR_CODES,
];

const DEAD_SESSION_CODES = new Set(Object.values(DEAD_SESSION_STATUS_CODES));

/** The status code an error carries, if it carries one.
 *
 * node-opcua puts it in different places depending on how the failure arrived:
 * on the error itself for a rejected service call, and on the response's service
 * result for a fault. Neither is guaranteed, which is why the text check below
 * still exists.
 */
function statusCodeOf(error: unknown): number | null {
  const candidate = error as {
    statusCode?: { value?: unknown };
    response?: { responseHeader?: { serviceResult?: { value?: unknown } } };
  } | null;
  const value =
    candidate?.statusCode?.value ?? candidate?.response?.responseHeader?.serviceResult?.value;
  return typeof value === "number" ? value : null;
}

/** True when `error` says the connection died rather than the request being wrong.
 *
 * Two kinds of evidence, strongest first. The **status code**, where the error
 * carries one — a number cannot be reworded by a release note, and this is the
 * only check here that is not string matching. Then the **text**, for everything
 * that arrives as prose, which by the time an error reaches a tool is most of it.
 *
 * Every entry names a failure of the connection rather than of the request,
 * which is what makes retrying on a fresh session meaningful: a
 * `BadNodeIdUnknown` would fail exactly the same way the second time.
 *
 * The Python server's `is_connection_error` answers the same question about the
 * same failures, so a retry that happens on one runtime happens on the other.
 */
export function isConnectionError(error: unknown): boolean {
  const code = statusCodeOf(error);
  if (code !== null && DEAD_SESSION_CODES.has(code)) return true;
  const message = error instanceof Error ? error.message : String(error ?? "");
  return DEAD_SESSION_MARKERS.some((marker) => message.includes(marker));
}

/** The message both runtimes give when a tool cannot be served at all.
 *
 * It names the endpoint, because the commonest cause is pointing at the wrong
 * one, and it names `get_server_status`, because that is the one tool that still
 * answers while the connection is down.
 */
export function notConnectedMessage(url: string, reason: string): string {
  return message("notConnected", { url, reason });
}

export class OpcuaConnection {
  private opcuaClient: OPCUAClient | null = null;
  private connectPromise: Promise<void> | null = null;
  /** The rebuild in flight, so concurrent callers join it rather than start one. */
  private reconnectPromise: Promise<void> | null = null;
  private state: ConnectionState = "disconnected";
  private lastError: string | null = null;
  /** An id for the session currently held; see `sessionId`. */
  private session_: string | null = null;
  session: ClientSession | null = null;

  /** Called with a *new* session after a dead one was replaced.
   *
   * Only on the rebuild path: node-opcua's own repair keeps the same session
   * object, so anything holding one stays valid and this is not called.
   */
  onSessionReplaced: ((session: ClientSession) => Promise<void>) | null = null;

  /** True while this server holds a session it believes is live. */
  get connected(): boolean {
    return this.state === "connected" && this.session !== null;
  }

  /** Why the connection is not up, in the client library's words. */
  get lastErrorMessage(): string | null {
    return this.lastError;
  }

  /** An id for the session this server holds, or null while it holds none.
   *
   * Not the OPC UA server's own SessionId. node-opcua exposes one and
   * python-opcua discards it, so a field built from it could not mean the same
   * thing on both runtimes — and the audit trail needs a field that does. This
   * is minted here when a session is established, which is what lets a record
   * answer "which of this process's sessions did the call ride on", and so lets
   * two writes either side of an outage be told apart.
   *
   * It is also what stops a burst of concurrent failures each rebuilding the
   * connection in turn: a caller passes the session its operation died on to
   * `reconnect`, and one that has already been replaced needs no second rebuild.
   */
  get sessionId(): string | null {
    return this.session_;
  }

  /** The endpoint this server is configured to talk to. */
  get endpointUrl(): string {
    return SERVER_URL;
  }

  async connect(): Promise<void> {
    if (this.opcuaClient && this.session && this.state === "connected") return;
    if (!this.connectPromise) {
      this.connectPromise = this.open().finally(() => {
        this.connectPromise = null;
      });
    }
    return this.connectPromise;
  }

  private async open(): Promise<void> {
    let client: OPCUAClient | null = null;
    this.state = "connecting";
    try {
      const security = securityConfig();
      for (const warning of securityWarnings(security)) {
        console.error(`WARNING: ${warning}`);
      }

      const reconnect = reconnectConfig();
      client = OPCUAClient.create({
        applicationName: "OPC UA MCP Client",
        connectionStrategy: {
          initialDelay: reconnect.initialDelay,
          maxDelay: reconnect.maxDelay,
          maxRetry: reconnect.maxRetry,
        },
        // Let node-opcua repair a broken channel and re-activate the session
        // rather than leaving a dropped connection for us to notice. Both are
        // its defaults; stating them keeps the behaviour this module's recovery
        // is built on from moving under us in a future release.
        keepSessionAlive: true,
        requestedSessionTimeout: reconnect.sessionTimeout,
        // What this client will let the server send it. node-opcua enforces both
        // itself; the Python runtime has to patch its library to do the same, and
        // the point of taking the numbers from the contract is that the two are
        // safe against the same thing. See transport-limits.ts.
        transportSettings: transportSettings(),
        ...clientSecurityOptions(security),
        endpoint_must_exist: false,
      });
      this.watch(client);

      await client.connect(SERVER_URL);
      console.error(`Connected to OPC UA server (${describeSecurity(security)})`);

      const session = await client.createSession(userIdentity(security));
      this.opcuaClient = client;
      this.session = session;
      this.session_ = randomBytes(8).toString("hex");
      this.state = "connected";
      this.lastError = null;
      console.error("OPC UA session created");

      // Read the NamespaceArray and hand it to the policy, every connect.
      //
      // A namespace *index* is assigned per session, so an allowlist pinned by
      // namespace URI (`nsu=…;i=5`) can only be resolved once the server has
      // said what its namespaces are — and a server that restarted may have
      // loaded them in a different order, which is the whole reason that form
      // exists. One read of one mandatory node; failing it is not fatal, but it
      // does leave URI-pinned entries unresolved, and the policy denies those.
      await this.bindPolicyNamespaces(session);
    } catch (error) {
      if (client) {
        try {
          await client.disconnect();
        } catch {
          // Preserve the original connection error.
        }
      }
      this.opcuaClient = null;
      this.session = null;
      this.state = "disconnected";
      this.lastError = error instanceof Error ? error.message : String(error);
      console.error("Failed to connect to OPC UA server:", error);
      throw error;
    }
  }

  /** Follow node-opcua's own view of the connection.
   *
   * Every handler checks that the event came from the *current* client: a
   * rebuild leaves the old one to finish emitting its `close`, and letting that
   * mark the new connection dead would undo the very repair that replaced it.
   */
  private watch(client: OPCUAClient): void {
    const isCurrent = () => this.opcuaClient === client;

    client.on("connection_lost", () => {
      if (!isCurrent()) return;
      this.state = "reconnecting";
      this.lastError = "connection lost";
      console.error("OPC UA connection lost — node-opcua is trying to repair it");
    });

    client.on("backoff", (retry: number, delay: number) => {
      if (!isCurrent()) return;
      console.error(`OPC UA reconnect: attempt ${retry + 1} failed, next try in ${delay}ms`);
    });

    client.on("connection_reestablished", () => {
      if (!isCurrent()) return;
      this.state = "connected";
      this.lastError = null;
      console.error("OPC UA connection re-established");
    });

    // A close we asked for never reaches here: `teardown` clears `opcuaClient`
    // before disconnecting, so `isCurrent()` is already false by then.
    client.on("close", (error?: Error | null) => {
      if (!isCurrent()) return;
      this.state = "disconnected";
      this.lastError = error ? error.message : "connection closed by the OPC UA server";
      console.error(`OPC UA connection closed${error ? `: ${error.message}` : ""}`);
    });
  }

  async disconnect(): Promise<void> {
    await this.teardown();
  }

  /** Drop the session and client, quietly. Shared by shutdown and rebuild. */
  private async teardown(): Promise<void> {
    if (this.connectPromise) {
      try {
        await this.connectPromise;
      } catch {
        // A failed open already cleaned up its partial client.
      }
    }

    const session = this.session;
    const client = this.opcuaClient;
    this.session = null;
    this.session_ = null;
    this.opcuaClient = null;
    this.state = "disconnected";

    if (session) {
      try {
        await session.close();
        console.error("OPC UA session closed");
      } catch (error) {
        console.error("Error closing OPC UA session:", error);
      }
    }

    if (client) {
      try {
        await client.disconnect();
        console.error("Disconnected from OPC UA server");
      } catch (error) {
        console.error("Error disconnecting OPC UA client:", error);
      }
    }
  }

  /** A live session, re-establishing one if the connection has gone. */
  async ensureConnection(): Promise<void> {
    if (this.connectPromise) {
      await this.connectPromise;
      return;
    }
    if (this.connected) return;
    if (this.state === "reconnecting" && (await this.awaitReconnection())) return;
    await this.reconnect();
  }

  /** Tell the tool policy which namespace URI is at which index on this server. */
  private async bindPolicyNamespaces(session: ClientSession): Promise<void> {
    try {
      const value = await session.readVariableValue(CONTRACT.diagnostics.namespaceArrayNodeId);
      const uris: unknown = value?.value?.value ?? null;
      toolPolicy().bindNamespaces(Array.isArray(uris) ? uris.map(String) : []);
    } catch (error) {
      console.error(
        `WARNING: could not read the server's NamespaceArray, so policy entries written as ` +
          `nsu=<uri>;… cannot be resolved and will be denied: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  /** Throw the client away and build a new one, whatever state it was in.
   *
   * The session that comes back is a new one, so anything holding the old one —
   * the subscriptions, above all — is told through `onSessionReplaced`.
   *
   * Single-flight, as one transaction, because the *steps* being individually
   * safe was not enough. `connect()` already memoised its promise, so two
   * concurrent rebuilds could not both open a client — but they could still
   * interleave as: A tears down, A opens a fresh session, B tears down and
   * closes the session A had just opened and was about to return. A then
   * believed it held a live session, and every call after it failed on one B had
   * closed (issue #107). The whole teardown → open → rebind → re-establish
   * sequence is therefore claimed once, and concurrent callers await the same
   * rebuild rather than starting a second.
   *
   * `stale` is the `sessionId` the caller's operation died on. If the connection
   * has already moved past it, the caller's need is met and this returns without
   * rebuilding: doing it again would tear down a session that is working and
   * re-attach every subscription on it for nothing. Without this, a burst of
   * concurrent failures — which is what an outage looks like from a server
   * serving several calls — rebuilt once per caller in turn (issue #111).
   */
  async reconnect(stale?: string | null): Promise<void> {
    if (stale != null && this.connected && this.session_ !== stale) return;
    if (!this.reconnectPromise) {
      this.reconnectPromise = this.rebuild().finally(() => {
        this.reconnectPromise = null;
      });
    }
    return this.reconnectPromise;
  }

  private async rebuild(): Promise<void> {
    await this.teardown();
    await this.connect();
    const session = this.session;
    if (session && this.onSessionReplaced) {
      try {
        await this.onSessionReplaced(session);
      } catch (error) {
        // Re-establishing what was being monitored is best-effort: failing here
        // would turn a recovered connection back into a failed tool call.
        console.error("Error re-establishing state on the new OPC UA session:", error);
      }
    }
  }

  /** Wait out node-opcua's own repair, for as long as the operator configured.
   *
   * Returns true if it finished in time. Returning false is not a failure — it
   * means the caller should stop waiting and rebuild, which is strictly more
   * likely to work than waiting longer on a client that may already have given
   * up.
   */
  private async awaitReconnection(): Promise<boolean> {
    const deadline = Date.now() + reconnectBudgetMs(reconnectConfig());
    while (Date.now() < deadline) {
      if (this.connected) return true;
      if (this.state !== "reconnecting") return false;
      await new Promise((resolve) => setTimeout(resolve, RECONNECT_POLL_MS));
    }
    return this.connected;
  }

  /** Run `operation` against a live session, once more on a fresh one if it dies.
   *
   * The retry exists because a connection can die between the check and the
   * call: `ensureConnection` can only report what was true a moment ago. Whether
   * running the operation again is *safe* is not this module's to judge — the
   * caller says so with `mayRepeat`, and a caller that says no still gets the
   * connection rebuilt, so the next call finds a live session.
   *
   * The tool dispatcher no longer comes through here. It has to re-authorize
   * between the two attempts — the fresh session may have renumbered the
   * namespaces the first attempt was authorized against (issue #105) — and it
   * reads the contract's own `retryPolicy` to decide what may follow a dead
   * session at all (issue #106), neither of which belongs in this module. What
   * is left is `get_server_status`, whose whole job is to reach for the
   * connection and report what it found.
   *
   * Errors are passed through as they came. `get_server_status` reports the
   * reason a connection failed as its own output and must not have it dressed
   * up; the tool dispatcher wraps it with `notConnectedMessage` instead.
   */
  async withRetry<T>(operation: () => Promise<T>, mayRepeat = true): Promise<T> {
    await this.ensureConnection();
    const session = this.sessionId;
    try {
      return await operation();
    } catch (error) {
      if (!isConnectionError(error)) throw error;
      console.error(
        `OPC UA call failed on a dead session; reconnecting${mayRepeat ? " and retrying once" : ""}`
      );
      await this.reconnect(session);
      if (!mayRepeat) throw error;
      return await operation();
    }
  }

  /** Whether the server reports HistoricalAccess.
   *
   * `on` is the session to ask, for a caller that already holds one. Passing it
   * is not an optimisation: this is called from `onSessionReplaced`, which runs
   * *inside* `reconnect()`, and `ensureConnection()` from there re-enters the
   * connect path it is standing in. The Python half takes its client the same
   * way and for the same reason.
   */
  async accessHistoryDataCapability(on?: ClientSession): Promise<boolean> {
    // Best-effort: never let an optional capability probe break tools/list. A
    // transient OPC UA outage should still leave the core tools advertised.
    try {
      let session = on;
      if (!session) {
        await this.ensureConnection();
        session = this.session!;
      }
      const dataValue = await session.readVariableValue(CONTRACT.capabilities.history.nodeId);
      return dataValue.statusCode === StatusCodes.Good && dataValue.value?.value === true;
    } catch (error) {
      console.error("accessHistoryDataCapability probe failed:", error);
      return false;
    }
  }

  /** The aggregate functions the server advertises, or none.
   *
   * `on` is the session to ask; see `accessHistoryDataCapability` for why that
   * matters rather than merely saving a call.
   */
  async serverCapabilitiesAggregateFunctions(on?: ClientSession): Promise<string[]> {
    // Best-effort: any failure (incl. a connection error) yields no aggregate
    // functions rather than breaking tools/list.
    let aggregateFunctions: string[] = [];
    try {
      let session = on;
      if (!session) {
        await this.ensureConnection();
        session = this.session!;
      }
      const browseResult = await session.browse({
        nodeId: CONTRACT.capabilities.aggregate.nodeId,
        browseDirection: 0, // Forward
        resultMask: 63, // All information (including BrowseName)
      });
      if (browseResult.statusCode === StatusCodes.Good && browseResult.references) {
        for (const reference of browseResult.references) {
          // Map the string BrowseName to the AggregateFunction
          if (reference.browseName.name) {
            const name = reference.browseName.name.toString();
            if (name in AggregateFunction) {
              aggregateFunctions.push(name);
            }
          }
        }
      }
    } catch (error) {
      console.error("Error during serverCapabilitiesAggregateFunctions:", error);
    }
    return aggregateFunctions;
  }
}
