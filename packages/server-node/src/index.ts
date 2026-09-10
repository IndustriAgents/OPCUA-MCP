#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { realpathSync } from "fs";
import { fileURLToPath, pathToFileURL } from "url";

import { OpcuaConnection } from "./connection.js";
import { VERSION } from "./contract.js";
import { parseArgs, runCli } from "./install.js";
import { securityConfig } from "./security.js";
import { OpcuaTools } from "./tools.js";

// Keep stdout pristine for the MCP stdio JSON-RPC transport: route any stray
// library logging (e.g. node-opcua PKI/certificate messages) to stderr.
//
// The CLI below therefore writes to `process.stdout` directly: when a human runs
// `--help` or `--dry-run`, its output *is* the program's result and belongs on
// stdout, which this remap would otherwise divert.
console.log = (...args: any[]) => console.error(...args);

/** Wires the MCP protocol surface to the OPC UA tools. */
class OPCUAMCPServer {
  private server: Server;
  private conn = new OpcuaConnection();
  private tools = new OpcuaTools(this.conn);

  constructor() {
    this.server = new Server(
      {
        name: "opcua-mcp-server",
        version: VERSION,
      },
      {
        capabilities: {
          tools: {},
        },
      }
    );

    this.setupToolHandlers();
    this.setupLifecycle();
  }

  private setupLifecycle() {
    // Handle shutdown gracefully
    process.on("SIGINT", async () => {
      await this.conn.disconnect();
      process.exit(0);
    });

    process.on("SIGTERM", async () => {
      await this.conn.disconnect();
      process.exit(0);
    });
  }

  private setupToolHandlers() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: await this.tools.listTools(),
    }));

    this.server.setRequestHandler(CallToolRequestSchema, async (request) =>
      this.tools.callTool(request)
    );
  }

  async run() {
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
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
    } catch (error) {
      console.error(`Configuration error: ${(error as Error).message}`);
      process.exit(1);
    }

    const server = new OPCUAMCPServer();
    server.run().catch(console.error);
    return;
  }
  process.exit(
    runCli(action, {
      execPath: process.execPath,
      scriptPath: opts.scriptPath,
      log: (msg) => process.stdout.write(`${msg}\n`),
      err: (msg) => process.stderr.write(`${msg}\n`),
    })
  );
}

if (isEntryPoint()) {
  // The module's own real path, not `process.argv[1]` — under a global install
  // the latter is the `node_modules/.bin` symlink, and a config entry pointing
  // at a symlink breaks as soon as the package is updated.
  runMain({ scriptPath: fileURLToPath(import.meta.url) });
}
