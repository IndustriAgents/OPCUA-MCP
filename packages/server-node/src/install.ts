// `opcua-mcp-server --install claude-desktop` — write the MCP client config so a
// user never has to find and hand-edit `claude_desktop_config.json`.
//
// The audience for this server is automation engineers, not Node developers, and
// "edit this JSON file, whose path differs per OS" is where they stall. This
// module resolves the path, merges an entry into whatever is already there, and
// writes it back atomically with a backup.
//
// The Python runtime ships the same subcommand with the same flags and produces
// the same config entry; tests/e2e/test_install_parity.py holds the two to it.

import { copyFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "fs";
import { homedir } from "os";
import { dirname, join, sep } from "path";

import { SERVER_URL } from "./config.js";
import { VERSION } from "./contract.js";

/** How an MCP client is told to launch this server. */
export interface ServerEntry {
  command: string;
  args?: string[];
  env: Record<string, string>;
}

/** The key this server is registered under in `mcpServers`. */
export const SERVER_KEY = "opcua";

/** Client identifiers accepted by `--install`. */
export const CLIENTS = ["claude-desktop"] as const;
export type Client = (typeof CLIENTS)[number];

/** Where Claude Desktop keeps its MCP server config, per platform.
 *
 * Returns null on a platform Claude Desktop does not ship for, so the caller can
 * fail with an explanation rather than writing a file nobody will read.
 */
export function claudeDesktopConfigPath(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir()
): string | null {
  switch (platform) {
    case "darwin":
      return join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json");
    case "win32":
      return join(
        env.APPDATA || join(home, "AppData", "Roaming"),
        "Claude",
        "claude_desktop_config.json"
      );
    case "linux":
      // Claude Desktop is unofficial on Linux, but the community builds follow
      // the XDG base directory spec, so honour XDG_CONFIG_HOME when it is set.
      return join(
        env.XDG_CONFIG_HOME || join(home, ".config"),
        "Claude",
        "claude_desktop_config.json"
      );
    default:
      return null;
  }
}

// Path segments that mark a package-runner's throwaway cache. A server installed
// from one of these is deleted the moment the cache is pruned, so writing its
// path into a config file produces an entry that works today and breaks later.
const EPHEMERAL_SEGMENTS = ["_npx"];
const EPHEMERAL_PREFIXES = ["dlx-"];

/** True when `path` sits inside an `npx`/`dlx` cache rather than a real install. */
export function isEphemeralInstall(path: string): boolean {
  return path
    .split(sep)
    .some(
      (seg) => EPHEMERAL_SEGMENTS.includes(seg) || EPHEMERAL_PREFIXES.some((p) => seg.startsWith(p))
    );
}

/** Build the `mcpServers` entry that launches this server.
 *
 * Three shapes, and which one you get matters:
 *
 *   * **The executable alone**, when `scriptPath` is null — the single-file
 *     build, where `execPath` is this program rather than a `node` that runs it.
 *   * **Absolute paths** (`/path/to/node /path/to/build/index.js`) whenever this
 *     process is running from a real install. Desktop apps are launched from the
 *     GUI, not a login shell, so they inherit a bare `PATH` — a bare `"node"` or
 *     `"npx"` command is the single most common reason an MCP server that works
 *     in the terminal fails to start in Claude Desktop, and it bites `nvm` users
 *     every time. Naming the interpreter and the script outright sidesteps both
 *     `PATH` lookup and the executable bit on the bin shim.
 *   * **`npx -y opcua-mcp-server`** when this process was itself launched by
 *     `npx`, because the absolute path in that case points into a cache that
 *     will be pruned out from under the config.
 */
export function serverEntry(opts: {
  execPath: string;
  scriptPath: string | null;
  url: string;
}): ServerEntry {
  const env = { OPCUA_SERVER_URL: opts.url };
  if (opts.scriptPath === null) {
    return { command: opts.execPath, env };
  }
  if (isEphemeralInstall(opts.scriptPath)) {
    return { command: "npx", args: ["-y", "opcua-mcp-server"], env };
  }
  return { command: opts.execPath, args: [opts.scriptPath], env };
}

/** Merge `entry` into a client config under `mcpServers[SERVER_KEY]`.
 *
 * Returns the new config and whether an existing entry was replaced. Other MCP
 * servers in the file are left exactly as they were — this is somebody's working
 * config, not ours to normalise.
 *
 * Throws when `mcpServers` exists but is not an object, rather than silently
 * discarding it.
 */
export function mergeServerEntry(
  config: unknown,
  entry: ServerEntry,
  { force = false }: { force?: boolean } = {}
): { config: Record<string, any>; replaced: boolean } {
  const base: Record<string, any> =
    config && typeof config === "object" && !Array.isArray(config)
      ? { ...(config as Record<string, any>) }
      : {};

  const existing = base.mcpServers;
  if (existing !== undefined && (typeof existing !== "object" || Array.isArray(existing))) {
    throw new Error("`mcpServers` in the config file is not an object — refusing to overwrite it");
  }

  const servers = { ...(existing as Record<string, any> | undefined) };
  const replaced = Object.prototype.hasOwnProperty.call(servers, SERVER_KEY);
  if (replaced && !force) {
    throw new Error(
      `an MCP server named "${SERVER_KEY}" is already configured — pass --force to replace it`
    );
  }

  servers[SERVER_KEY] = entry;
  base.mcpServers = servers;
  return { config: base, replaced };
}

/** Parsed `--install` invocation. */
export interface InstallOptions {
  client: Client;
  url: string;
  force: boolean;
  dryRun: boolean;
}

export const USAGE = `opcua-mcp-server — MCP server for OPC UA

Usage:
  opcua-mcp-server                      Run the MCP server on stdio (default)
  opcua-mcp-server --install <client>   Register this server with an MCP client
  opcua-mcp-server --version            Print the version
  opcua-mcp-server --help               Show this help

Install options:
  --install <client>   One of: ${CLIENTS.join(", ")}
  --url <endpoint>     OPC UA endpoint to record
                       (default: $OPCUA_SERVER_URL, else opc.tcp://localhost:4840)
  --force              Replace an existing "${SERVER_KEY}" entry
  --dry-run            Print the resulting config instead of writing it
`;

/** What the caller should do after `parseArgs`. */
export type Action =
  | { kind: "serve" }
  | { kind: "help" }
  | { kind: "version" }
  | { kind: "install"; options: InstallOptions }
  | { kind: "error"; message: string };

/** Parse CLI arguments (everything after the script path).
 *
 * No arguments means "run the server", which is how every MCP client invokes us;
 * the flags exist for humans at a terminal.
 */
export function parseArgs(argv: string[], defaultUrl: string = SERVER_URL): Action {
  if (argv.length === 0) return { kind: "serve" };

  let client: string | undefined;
  let url = defaultUrl;
  let force = false;
  let dryRun = false;

  /** The operand after a flag, or undefined when the flag was given none.
   *
   * A token starting with `-` counts as "none". In `--url --dry-run` the user
   * forgot the endpoint, and taking `--dry-run` as the URL would write a real
   * config with a nonsense endpoint *and* swallow the very flag that was meant
   * to stop it writing anything. Python's argparse rejects that input, and the
   * two runtimes have to agree.
   */
  const operandAfter = (i: number): string | undefined => {
    const next = argv[i + 1];
    return next === undefined || next.startsWith("-") ? undefined : next;
  };

  for (let i = 0; i < argv.length; i++) {
    // `--url=value` as well as `--url value`. Python's argparse accepts both, and
    // a user who reads the Python docs and types the first form at the Node
    // runtime must not be told it is an unknown argument.
    const token = argv[i];
    const eq = token.startsWith("--") ? token.indexOf("=") : -1;
    const arg = eq === -1 ? token : token.slice(0, eq);
    const inline = eq === -1 ? undefined : token.slice(eq + 1);

    /** This flag's operand, from `=value` or the next token, consuming it. */
    const operand = (): string | undefined => {
      if (inline !== undefined) return inline;
      const next = operandAfter(i);
      if (next !== undefined) i++;
      return next;
    };

    /** A value attached to a flag that takes none — argparse rejects this too. */
    const rejectsInline = (): Action | undefined =>
      inline === undefined ? undefined : { kind: "error", message: `${arg} takes no value` };

    switch (arg) {
      case "-h":
      case "--help":
        return { kind: "help" };
      case "-v":
      case "--version":
        return { kind: "version" };
      case "--install": {
        const value = operand();
        if (value === undefined) {
          return { kind: "error", message: "--install needs a client name" };
        }
        client = value;
        break;
      }
      case "--url": {
        const value = operand();
        if (value === undefined) return { kind: "error", message: "--url needs an endpoint" };
        url = value;
        break;
      }
      case "--force": {
        const bad = rejectsInline();
        if (bad) return bad;
        force = true;
        break;
      }
      case "--dry-run": {
        const bad = rejectsInline();
        if (bad) return bad;
        dryRun = true;
        break;
      }
      default:
        return { kind: "error", message: `unknown argument: ${arg}` };
    }
  }

  if (client === undefined) {
    return { kind: "error", message: "nothing to do — pass --install <client> or --help" };
  }
  if (!(CLIENTS as readonly string[]).includes(client)) {
    return {
      kind: "error",
      message: `unknown client: ${client} (expected one of: ${CLIENTS.join(", ")})`,
    };
  }

  return { kind: "install", options: { client: client as Client, url, force, dryRun } };
}

/** Timestamp suffix for a backup file: sortable, filename-safe, second-resolution. */
function backupSuffix(now: Date = new Date()): string {
  return now.toISOString().replace(/[-:]/g, "").replace(/\..*$/, "");
}

/** Read a client config file, tolerating absence but not corruption.
 *
 * An unparseable config is a hard error: overwriting it would silently destroy
 * every other MCP server the user has configured.
 */
function readConfig(path: string): unknown {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch (err: any) {
    if (err?.code === "ENOENT") return {};
    throw err;
  }
  if (raw.trim() === "") return {};
  try {
    return JSON.parse(raw);
  } catch (err: any) {
    throw new Error(`${path} is not valid JSON (${err.message}) — fix or move it, then retry`);
  }
}

/** Write `config` to `path` atomically, backing up anything already there. */
function writeConfig(path: string, config: unknown): string | null {
  mkdirSync(dirname(path), { recursive: true });

  let backup: string | null = null;
  try {
    backup = `${path}.bak-${backupSuffix()}`;
    copyFileSync(path, backup);
  } catch (err: any) {
    if (err?.code !== "ENOENT") throw err;
    backup = null; // nothing was there to back up
  }

  // Write-then-rename: a crash mid-write must not leave a truncated config that
  // takes every other MCP server down with it.
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, `${JSON.stringify(config, null, 2)}\n`, "utf8");
  renameSync(tmp, path);
  return backup;
}

/** Run `--install`, reporting what it did. Returns a process exit code. */
export function runInstall(
  options: InstallOptions,
  io: {
    execPath: string;
    scriptPath: string | null;
    log: (msg: string) => void;
    /** Overrides the resolved client config path; for tests. */
    configPath?: string;
  }
): number {
  const path = io.configPath ?? claudeDesktopConfigPath();
  if (path === null) {
    io.log(`Claude Desktop has no known config location on ${process.platform}.`);
    return 1;
  }

  const entry = serverEntry({ execPath: io.execPath, scriptPath: io.scriptPath, url: options.url });

  let merged: { config: Record<string, any>; replaced: boolean };
  try {
    merged = mergeServerEntry(readConfig(path), entry, { force: options.force });
  } catch (err: any) {
    io.log(`Error: ${err.message}`);
    return 1;
  }

  if (options.dryRun) {
    io.log(`Would write ${path}:`);
    io.log(JSON.stringify(merged.config, null, 2));
    return 0;
  }

  const backup = writeConfig(path, merged.config);
  io.log(`${merged.replaced ? "Replaced" : "Added"} MCP server "${SERVER_KEY}" in ${path}`);
  if (backup) io.log(`Previous config backed up to ${backup}`);
  io.log(`OPC UA endpoint: ${options.url}`);
  io.log("Restart Claude Desktop for the change to take effect.");
  return 0;
}

/** Handle a non-serving CLI action. Returns a process exit code. */
export function runCli(
  action: Exclude<Action, { kind: "serve" }>,
  io: {
    execPath: string;
    scriptPath: string | null;
    log: (msg: string) => void;
    err: (msg: string) => void;
    configPath?: string;
  }
): number {
  switch (action.kind) {
    case "help":
      io.log(USAGE);
      return 0;
    case "version":
      io.log(VERSION);
      return 0;
    case "error":
      io.err(`opcua-mcp-server: ${action.message}`);
      io.err(USAGE);
      return 2;
    case "install":
      return runInstall(action.options, io);
  }
}
