#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListResourcesRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { realpathSync } from "fs";
import { fileURLToPath, pathToFileURL } from "url";

import { AuditSink, describeAudit, parseAuditConfig } from "./audit.js";
import { SERVER_URL, describeReconnect, reconnectConfig } from "./config.js";
import { OpcuaConnection } from "./connection.js";
import { VERSION } from "./contract.js";
import { parseArgs, runCli } from "./install.js";
import { describePolicy, toolPolicy } from "./policy.js";
import { securityConfig } from "./security.js";
import { OpcuaTools } from "./tools.js";

// Keep stdout pristine for the MCP stdio JSON-RPC transport: route any stray
// library logging (e.g. node-opcua PKI/certificate messages) to stderr.
//
// The CLI below therefore writes to `process.stdout` directly: when a human runs
// `--help` or `--dry-run`, its output *is* the program's result and belongs on
// stdout, which this remap would otherwise divert.
console.log = (...args: any[]) => console.error(...args);

/** How long an exiting server waits for the OPC UA side to close cleanly. */
const SHUTDOWN_GRACE_MS = 5_000;

/** Wires the MCP protocol surface to the OPC UA tools. */
class OPCUAMCPServer {
  private server: Server;
  private conn: OpcuaConnection;
  private tools: OpcuaTools;
  private closing: Promise<void> | undefined;

  constructor(audit: AuditSink = new AuditSink()) {
    // One policy object, wired into both halves rather than fetched twice.
    //
    // This is the part of #116 that is not cosmetic. The connection re-binds the
    // policy's namespace mapping on every (re)connect, and the tools authorize
    // against it — so they have to be the *same* object. They used to be, only
    // because `toolPolicy()` memoised one for the process: two independent calls
    // that happened to return one instance. Constructing it here and passing it
    // down makes that a property of the wiring instead of a property of a cache,
    // which is what lets a second instance exist without the two silently
    // authorizing against different namespace mappings.
    const policy = toolPolicy();
    this.conn = new OpcuaConnection(SERVER_URL, policy, reconnectConfig());
    this.tools = new OpcuaTools(this.conn, policy, audit);
    this.server = new Server(
      {
        name: "opcua-mcp-server",
        version: VERSION,
      },
      {
        capabilities: {
          tools: {},
          // Read-only: the agent re-reads `opcua://subscriptions` to see what
          // the OPC UA subscriptions have delivered. `subscribe` is deliberately
          // absent — see docs/architecture.md for why change notifications are
          // not offered on either runtime.
          resources: {},
        },
      }
    );

    this.setupToolHandlers();
    this.setupResourceHandlers();
    this.setupLifecycle();
  }

  private setupLifecycle() {
    // Handle shutdown gracefully
    process.on("SIGINT", () => void this.exitAfterShutdown());
    process.on("SIGTERM", () => void this.exitAfterShutdown());

    // Without this, every subscription would be left for the OPC UA server to
    // expire on its own when the transport closes.
    this.server.onclose = () => {
      void this.shutdown();
    };
  }

  /** Drop the OPC UA subscriptions, then the session. In that order. Once.
   *
   * `close`, not `disconnect`: a connection round still dialling a plant that is
   * down is aborted rather than waited out, so shutting down takes as long as
   * closing a session and not as long as the configured backoff (#136).
   */
  private shutdown(): Promise<void> {
    this.closing ??= (async () => {
      await this.tools.shutdown();
      await this.conn.close();
    })();
    return this.closing;
  }

  /** Shut down, then exit — even if the OPC UA side never answers. */
  private async exitAfterShutdown(): Promise<never> {
    // Bounded: disconnecting from a server that has gone quiet can wait on a
    // request timeout, and a client that has left is not waiting for us.
    const deadline = new Promise<void>((resolve) => setTimeout(resolve, SHUTDOWN_GRACE_MS).unref());
    await Promise.race([this.shutdown().catch(() => undefined), deadline]);
    process.exit(0);
  }

  private setupToolHandlers() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: await this.tools.listTools(),
    }));

    this.server.setRequestHandler(CallToolRequestSchema, async (request) =>
      this.tools.callTool(request)
    );
  }

  private setupResourceHandlers() {
    this.server.setRequestHandler(ListResourcesRequestSchema, async () => ({
      resources: this.tools.listResources(),
    }));

    this.server.setRequestHandler(ReadResourceRequestSchema, async (request) =>
      this.tools.readResource(request.params.uri)
    );
  }

  async run() {
    // Started, not awaited: the transport opens whatever the plant is doing.
    // `tools/list` no longer opens the connection itself, so something has to —
    // but awaiting it here held back `initialize` for the whole first connection
    // round, and with OPCUA_RECONNECT_MAX_RETRY=-1 that round never ended, so the
    // MCP client saw a server that never started (#136). Plant connectivity is
    // runtime state, reported by `get_server_status`; it does not gate the
    // protocol.
    //
    // It had been moved in front of the transport for a reason that still holds:
    // requests served *during* the warm-up used to see no session and answer as
    // though the server supported nothing. `tools/list` and `get_server_status`
    // therefore wait for it — bounded, see `OpcuaTools.awaitWarmUp` — and a tool
    // call that needs a session joins its connection round. The Python runtime
    // starts its warm-up the same way, from its lifespan.
    //
    // Never fatal: a plant that is unreachable simply means the optional tools
    // appear once a tool call has brought the connection up.
    void this.tools.startWarmUp();
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
    // The usual end of an MCP session is not a signal at all: the client closes
    // stdin. The SDK's stdio transport listens for data and errors on stdin but
    // not for its end, so `onclose` never fires — and with an OPC UA session
    // open, its keep-alive timers held the event loop and the process outlived
    // its client as an orphan holding that session. The binary smoke test's
    // clean-exit check found it (#142); the Python runtime already exits here.
    process.stdin.once("end", () => void this.exitAfterShutdown());
    console.error("OPC UA MCP Server running on stdio");
  }
}

// Exported for unit tests. Importing this module must stay side-effect free
// apart from reading build/ assets — the server is only started below, and only
// when this file is the process entry point.
export { OPCUAMCPServer };

/** True when this module is the entry point rather than an import.
 *
 * `process.argv[1]` keeps the path as invoked, which for an npm-installed CLI is
 * the `node_modules/.bin` symlink, while `import.meta.url` is always the resolved
 * real path. Comparing them directly would therefore be false under `npx` and the
 * server would silently never start; `realpathSync` collapses that difference.
 */
function isEntryPoint(): boolean {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(entry)).href;
  } catch {
    return false;
  }
}

/** Serve, or handle a CLI flag — the whole of what running this program does.
 *
 * Shared by the two ways this code is shipped, which differ only in how the
 * program locates itself: the npm package is a script run by some `node` on the
 * machine, while the single-file executable *is* the interpreter and has no
 * script path at all (`scriptPath: null`). `--install` needs to know the
 * difference, because it writes that location into a client config.
 */
export function runMain(opts: { scriptPath: string | null }): void {
  // MCP clients invoke us with no arguments, which is the `serve` path. Anything
  // else came from a human at a terminal.
  const action = parseArgs(process.argv.slice(2));
  if (action.kind === "serve") {
    let audit: AuditSink;
    // Fail fast and readably on a bad security configuration: an MCP client only
    // ever shows the server's stderr, so an unhandled parse error deep in a
    // capability probe would surface as "server exited" and nothing else.
    //
    // Deliberately only on this path. `--help` and `--install` have to keep
    // working while the security configuration is still being got right —
    // refusing to print the help until the certificate is valid would be
    // backwards, and `--install` is often how the endpoint gets configured in
    // the first place.
    try {
      securityConfig();
      // Opened here and not lazily: an operator who set OPCUA_AUDIT_FILE and
      // cannot be given one has to be told now, not at the first control call
      // they were relying on it to record. So does one whose target is unsafe
      // to write (a symlink, another account's file) or whose chain key cannot
      // be read.
      audit = AuditSink.fromConfig(parseAuditConfig());
      console.error(`Tool policy: ${describePolicy(toolPolicy())}`);
      console.error(`Control audit: ${describeAudit(audit)}`);
      console.error(`Connection resilience: ${describeReconnect(reconnectConfig())}`);
    } catch (error) {
      console.error(`Configuration error: ${(error as Error).message}`);
      process.exit(1);
    }

    const server = new OPCUAMCPServer(audit);
    server.run().catch(console.error);
    return;
  }
  // `process.exitCode`, never `process.exit()`. Writes to a *piped* stdout are
  // asynchronous, and `process.exit()` does not wait for them — piping a
  // `--dry-run` of a large config to a file would truncate the JSON mid-write
  // and still report success. Setting the code lets Node drain and exit on its
  // own, which it can do immediately here because the CLI path opens nothing:
  // no MCP transport, no OPC UA connection, only synchronous file IO.
  process.exitCode = runCli(action, {
    execPath: process.execPath,
    scriptPath: opts.scriptPath,
    log: (msg) => process.stdout.write(`${msg}\n`),
    err: (msg) => process.stderr.write(`${msg}\n`),
  });
}

if (isEntryPoint()) {
  // The module's own real path, not `process.argv[1]` — under a global install
  // the latter is the `node_modules/.bin` symlink, and a config entry pointing
  // at a symlink breaks as soon as the package is updated.
  runMain({ scriptPath: fileURLToPath(import.meta.url) });
}
