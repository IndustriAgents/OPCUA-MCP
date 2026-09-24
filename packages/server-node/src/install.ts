// `opcua-mcp-server --install <client>` — write the MCP client config so a user
// never has to find and hand-edit `claude_desktop_config.json` or
// `~/.codex/config.toml`.
//
// The audience for this server is automation engineers, not Node developers, and
// "edit this JSON file, whose path differs per OS" is where they stall. This
// module resolves the path, merges an entry into whatever is already there, and
// writes it back atomically with a backup.
//
// It is also the easiest way to configure the server, so it must be able to say
// everything a secure deployment needs (#135). Its setting flags are therefore
// built from `/contract/config.json` rather than listed here, the configuration
// it writes is run through the server's own startup parsers before anything is
// written, and a control profile aimed at a remote endpoint nobody verifies is
// refused unless the user overrides that explicitly.
//
// The Python runtime ships the same subcommand with the same flags, refusals and
// warnings; tests/fixtures/install-cases.json is the table both are held to.

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "fs";
import { homedir } from "os";
import { dirname, join, resolve } from "path";

import { runVerify } from "./audit.js";
import { SERVER_URL, parseReconnectConfig } from "./config.js";
import { CONTRACT, VERSION, configSchema, type ConfigSetting } from "./contract.js";
import { ToolPolicy, parsePolicyConfig } from "./policy.js";
import { parseSecurityConfig } from "./security.js";

/** How an MCP client is told to launch this server. */
export interface ServerEntry {
  command: string;
  args?: string[];
  env: Record<string, string>;
  /** Codex only: variables passed through from the client's own environment. */
  env_vars?: string[];
}

/** The key this server is registered under in the client config. */
export const SERVER_KEY = "opcua";

/** Client identifiers accepted by `--install`. */
export const CLIENTS = ["claude-desktop", "codex"] as const;
export type Client = (typeof CLIENTS)[number];

/** Stands in for a sensitive value in anything printed. */
export const REDACTED = "<redacted>";

/** The one secret the installer ever handles, and only from the environment. */
const PASSWORD_ENV = "OPCUA_PASSWORD";

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

/** Where Codex keeps its config: `$CODEX_HOME/config.toml`, else `~/.codex/`. */
export function codexConfigPath(
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir()
): string {
  return join(env.CODEX_HOME || join(home, ".codex"), "config.toml");
}

/** The config file `client` reads on this machine, or null if it has none here. */
export function clientConfigPath(
  client: Client,
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir()
): string | null {
  return client === "codex"
    ? codexConfigPath(env, home)
    : claudeDesktopConfigPath(platform, env, home);
}

/** Human name of a client, for messages. */
function clientName(client: Client): string {
  return client === "codex" ? "Codex" : "Claude Desktop";
}

// Path segments that mark a package-runner's throwaway cache. A server installed
// from one of these is deleted the moment the cache is pruned, so writing its
// path into a config file produces an entry that works today and breaks later.
const EPHEMERAL_SEGMENTS = ["_npx"];
const EPHEMERAL_PREFIXES = ["dlx-"];

/** True when `path` sits inside an `npx`/`dlx` cache rather than a real install.
 *
 * Split on either separator rather than on the host's `sep`. Windows accepts
 * both, and a path reaching this function has come from `process.argv[1]` or a
 * caller — not necessarily from `path.join` — so a forward-slashed Windows path
 * would have been one segment, matched nothing, and written a cache path into
 * somebody's config as though it were a real install. The Python half is already
 * separator-agnostic for free: `Path(...).parts` splits both on Windows.
 */
export function isEphemeralInstall(path: string): boolean {
  return path
    .split(/[\\/]/)
    .some(
      (seg) => EPHEMERAL_SEGMENTS.includes(seg) || EPHEMERAL_PREFIXES.some((p) => seg.startsWith(p))
    );
}

/** Build the entry that launches this server with `env`.
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
  env: Record<string, string>;
}): ServerEntry {
  const env = { ...opts.env };
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
    throw new InstallRefusal(
      "unreadable-config",
      "`mcpServers` in the config file is not an object — refusing to overwrite it"
    );
  }

  const servers = { ...(existing as Record<string, any> | undefined) };
  const replaced = Object.prototype.hasOwnProperty.call(servers, SERVER_KEY);
  if (replaced && !force) {
    throw alreadyConfigured();
  }

  servers[SERVER_KEY] = entry;
  base.mcpServers = servers;
  return { config: base, replaced };
}

function alreadyConfigured(): InstallRefusal {
  return new InstallRefusal(
    "already-configured",
    `an MCP server named "${SERVER_KEY}" is already configured — pass --force to replace it`
  );
}

// --- Codex: TOML -----------------------------------------------------------------
//
// Codex keeps its MCP servers in TOML, and neither runtime ships a TOML writer.
// The installer does not need one: it owns exactly one table, `[mcp_servers.opcua]`
// and its subtables, so it removes those and appends its own, leaving every other
// byte of the user's file as it was. What it cannot safely rewrite — the entry
// spelled as dotted keys or an inline table — it refuses rather than guesses at.

/** A TOML basic string. Escapes the same characters in both runtimes. */
export function tomlString(value: string): string {
  let out = '"';
  for (const ch of value) {
    const code = ch.codePointAt(0)!;
    if (ch === "\\") out += "\\\\";
    else if (ch === '"') out += '\\"';
    else if (code < 0x20 || code === 0x7f) out += `\\u${code.toString(16).padStart(4, "0")}`;
    else out += ch;
  }
  return `${out}"`;
}

function tomlArray(values: string[]): string {
  return `[${values.map(tomlString).join(", ")}]`;
}

/** The `[mcp_servers.opcua]` block for `entry`. */
export function codexBlock(entry: ServerEntry): string {
  const lines = [`[mcp_servers.${SERVER_KEY}]`, `command = ${tomlString(entry.command)}`];
  if (entry.args) lines.push(`args = ${tomlArray(entry.args)}`);
  if (entry.env_vars?.length) lines.push(`env_vars = ${tomlArray(entry.env_vars)}`);
  lines.push("", `[mcp_servers.${SERVER_KEY}.env]`);
  for (const [name, value] of Object.entries(entry.env)) {
    lines.push(`${name} = ${tomlString(value)}`);
  }
  return `${lines.join("\n")}\n`;
}

const TOML_HEADER = /^\s*\[\[?\s*([^\]]*?)\s*\]\]?\s*(#.*)?$/;

/** A table header's dotted key with quoting and spacing normalised away. */
function tomlHeaderKey(line: string): string | null {
  const match = TOML_HEADER.exec(line);
  if (!match) return null;
  return match[1]
    .split(".")
    .map((part) => part.trim().replace(/^(["'])(.*)\1$/, "$2"))
    .join(".");
}

/** Replace or append this server's block in a Codex `config.toml`. */
export function mergeCodexConfig(
  text: string,
  entry: ServerEntry,
  { force = false }: { force?: boolean } = {}
): { text: string; replaced: boolean } {
  const ours = `mcp_servers.${SERVER_KEY}`;
  const lines = text.split(/\r?\n/);
  const kept: string[] = [];
  let replaced = false;
  let inOurs = false;
  let table = "";
  for (const line of lines) {
    const key = tomlHeaderKey(line);
    if (key !== null) {
      table = key;
      inOurs = key === ours || key.startsWith(`${ours}.`);
      if (inOurs) replaced = true;
    } else if (!inOurs) {
      // The same server spelled some other way — `opcua = {...}` under
      // [mcp_servers], or `mcp_servers.opcua.command = ...` at the top level.
      // Rewriting either correctly needs a real TOML parser; refusing does not.
      const assignment = /^\s*([A-Za-z0-9_."'-]+?)\s*=/.exec(line)?.[1];
      if (assignment !== undefined) {
        const dotted = (table ? `${table}.` : "") + assignment.replace(/["']/g, "");
        if (dotted === ours || dotted.startsWith(`${ours}.`)) {
          throw new InstallRefusal(
            "unreadable-config",
            `the "${SERVER_KEY}" server in this file is written as dotted keys or an inline ` +
              `table, which --install does not rewrite — remove it by hand, then retry`
          );
        }
      }
    }
    if (!inOurs) kept.push(line);
  }
  if (replaced && !force) throw alreadyConfigured();

  while (kept.length > 0 && kept[kept.length - 1].trim() === "") kept.pop();
  const head = kept.length > 0 ? `${kept.join("\n")}\n\n` : "";
  return { text: head + codexBlock(entry), replaced };
}

// --- the settings the flags are built from ---------------------------------------

/** Every setting `--install` offers a flag for, in schema order. */
export function installerSettings(): ConfigSetting[] {
  // `secret` is excluded here as well as by the schema test: a flag's value is a
  // process argument, which any local user can read.
  return configSchema().settings.filter((s) => s.surfaces.includes("installer") && !s.secret);
}

/** A setting's `--flag`: its stable key with dashes. */
export function flagFor(setting: ConfigSetting): string {
  return `--${setting.key.replace(/_/g, "-")}`;
}

/** Parsed `--install` invocation. */
export interface InstallOptions {
  client: Client;
  /** Raw flag values keyed by schema key; a boolean flag's value is "true". */
  settings: Record<string, string>;
  force: boolean;
  dryRun: boolean;
  /** Write $OPCUA_PASSWORD into the client config in plain text. */
  storePasswordInConfig: boolean;
  /** Write a control profile for a remote endpoint with no pinned certificate. */
  allowUnverifiedRemoteControl: boolean;
}

/** `--help` text. The setting flags are listed from the schema, by category.
 *
 * A function, not a constant: `index.ts` imports this module on the serving
 * path too, and the schema must not be read — or able to fail — at import. */
export function usage(): string {
  const lines = [
    "opcua-mcp-server — MCP server for OPC UA",
    "",
    "Usage:",
    "  opcua-mcp-server                      Run the MCP server on stdio (default)",
    "  opcua-mcp-server --install <client>   Register this server with an MCP client",
    "  opcua-mcp-server --version            Print the version",
    "  opcua-mcp-server --verify-audit <file>... [--key-file <key>]",
    "                                        Check an OPCUA_AUDIT_FILE hash chain",
    "                                        (rotated files oldest first)",
    "  opcua-mcp-server --help               Show this help",
    "",
    "Install options:",
    `  ${"--install <client>".padEnd(38)}One of: ${CLIENTS.join(", ")}`,
    `  ${"--url <endpoint>".padEnd(38)}Same as --server-url`,
    `  ${"--force".padEnd(38)}Replace an existing "${SERVER_KEY}" entry`,
    `  ${"--dry-run".padEnd(38)}Validate, print a redacted preview, write nothing`,
    `  ${"--store-password-in-config".padEnd(38)}Copy $OPCUA_PASSWORD into the file (plain text)`,
    `  ${"--allow-unverified-remote-control".padEnd(38)}Write operator/full for a remote endpoint`,
    `  ${"".padEnd(38)}with no pinned --server-cert (lab only)`,
  ];
  const { categories } = configSchema();
  let category = "";
  for (const setting of installerSettings()) {
    if (setting.category !== category) {
      category = setting.category;
      lines.push("", `${categories[category]?.title ?? category}:`);
    }
    const operand = setting.type === "boolean" ? "" : ` <${setting.type}>`;
    lines.push(`  ${`${flagFor(setting)}${operand}`.padEnd(38)}${setting.env}`);
  }
  lines.push(
    "",
    "Passwords are never accepted as flags. See docs/install.md for how to supply one."
  );
  return `${lines.join("\n")}\n`;
}

/** What the caller should do after `parseArgs`. */
export type Action =
  | { kind: "serve" }
  | { kind: "help" }
  | { kind: "version" }
  | { kind: "install"; options: InstallOptions }
  | { kind: "verify-audit"; argv: string[] }
  | { kind: "error"; message: string };

/** Refusal for a secret passed as a flag. Never echoes the value. */
function secretFlagMessage(flag: string, env: string): string {
  return (
    `${flag} is not accepted: anything on a command line is visible to every local user ` +
    `and lands in shell history. Leave the password out and see docs/install.md, or ` +
    `export ${env} and pass --store-password-in-config`
  );
}

/** argparse's rule for a token that looks like a negative number: a value, not a flag. */
const NEGATIVE_NUMBER = /^-\d+$|^-\d*\.\d+$/;

/** Parse CLI arguments (everything after the script path).
 *
 * No arguments means "run the server", which is how every MCP client invokes us;
 * the flags exist for humans at a terminal.
 */
export function parseArgs(argv: string[], defaultUrl: string = SERVER_URL): Action {
  if (argv.length === 0) return { kind: "serve" };
  // Its own grammar (files, then an optional key), so it is routed before the
  // install flags rather than taught to them. Needs no OPC UA server.
  if (argv[0] === "--verify-audit") return { kind: "verify-audit", argv: argv.slice(1) };

  let client: string | undefined;
  const settings: Record<string, string> = {};
  let force = false;
  let dryRun = false;
  let storePasswordInConfig = false;
  let allowUnverifiedRemoteControl = false;

  const byFlag = new Map(installerSettings().map((s) => [flagFor(s), s]));
  const secretFlags = new Map(
    configSchema()
      .settings.filter((s) => s.secret)
      .map((s) => [flagFor(s), s])
  );

  /** The operand after a flag, or undefined when the flag was given none.
   *
   * A token starting with `-` counts as "none". In `--url --dry-run` the user
   * forgot the endpoint, and taking `--dry-run` as the URL would write a real
   * config with a nonsense endpoint *and* swallow the very flag that was meant
   * to stop it writing anything. Python's argparse rejects that input, and the
   * two runtimes have to agree — including on its one exception, a negative
   * number, which `--reconnect-max-retry -1` needs.
   */
  const operandAfter = (i: number): string | undefined => {
    const next = argv[i + 1];
    if (next === undefined) return undefined;
    return next.startsWith("-") && !NEGATIVE_NUMBER.test(next) ? undefined : next;
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

    const booleanFlag = (): Action | undefined => rejectsInline();

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
        continue;
      }
      case "--url": {
        const value = operand();
        if (value === undefined) return { kind: "error", message: "--url needs an endpoint" };
        settings.server_url = value;
        continue;
      }
      case "--force":
      case "--dry-run":
      case "--store-password-in-config":
      case "--allow-unverified-remote-control": {
        const bad = booleanFlag();
        if (bad) return bad;
        if (arg === "--force") force = true;
        else if (arg === "--dry-run") dryRun = true;
        else if (arg === "--store-password-in-config") storePasswordInConfig = true;
        else allowUnverifiedRemoteControl = true;
        continue;
      }
    }

    const secret = secretFlags.get(arg);
    if (secret) return { kind: "error", message: secretFlagMessage(arg, secret.env) };

    const setting = byFlag.get(arg);
    if (!setting) return { kind: "error", message: `unknown argument: ${arg}` };
    if (setting.type === "boolean") {
      const bad = booleanFlag();
      if (bad) return bad;
      settings[setting.key] = "true";
    } else {
      const value = operand();
      if (value === undefined) return { kind: "error", message: `${arg} needs a value` };
      settings[setting.key] = value;
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
  settings.server_url ??= defaultUrl;

  return {
    kind: "install",
    options: {
      client: client as Client,
      settings,
      force,
      dryRun,
      storePasswordInConfig,
      allowUnverifiedRemoteControl,
    },
  };
}

// --- planning: everything short of touching the client config --------------------

/** Why an install was refused. `exitCode` 2 is a usage error, 1 everything else. */
export class InstallRefusal extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly exitCode: number = 1
  ) {
    super(message);
  }
}

/** One thing a user should know about the config before relying on it. */
export interface Finding {
  code: string;
  message: string;
}

/** A validated configuration, ready to merge into a client config. */
export interface InstallPlan {
  /** The environment the entry carries, in schema order. */
  env: Record<string, string>;
  /** Variables the client passes through from its own environment (Codex). */
  envVars: string[];
  summary: string[];
  warnings: Finding[];
}

/** The host of an `opc.tcp://host:port/path` URL, lowercased; "" if none. */
export function endpointHost(url: string): string {
  const afterScheme = url.includes("://") ? url.slice(url.indexOf("://") + 3) : "";
  let authority = afterScheme.split(/[/?#]/)[0];
  authority = authority.slice(authority.lastIndexOf("@") + 1);
  if (authority.startsWith("[")) return authority.slice(1, authority.indexOf("]")).toLowerCase();
  return authority.split(":")[0].toLowerCase();
}

/** Whether `url` names this machine. Anything unparseable counts as remote. */
export function isLoopbackEndpoint(url: string): boolean {
  const host = endpointHost(url);
  return (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host) ||
    host === "::1" ||
    host === "0:0:0:0:0:0:0:1"
  );
}

/** One flag value, checked and normalised as its schema entry says. */
function normalise(setting: ConfigSetting, raw: string, cwd: string): string {
  const flag = flagFor(setting);
  const value = raw.trim();
  if (setting.type === "boolean") return "true";
  if (value === "") throw new InstallRefusal("invalid-value", `${flag} needs a value`, 2);

  switch (setting.type) {
    case "enum": {
      const lower = value.toLowerCase();
      const aliases = setting.choiceAliases ?? {};
      const alias = Object.keys(aliases).find((a) => a.toLowerCase() === lower);
      const canonical =
        alias !== undefined
          ? aliases[alias]
          : (setting.choices ?? []).find((c) => c.toLowerCase() === lower);
      if (canonical === undefined) {
        const accepted = [...(setting.choices ?? []), ...Object.keys(aliases)];
        throw new InstallRefusal(
          "invalid-value",
          `${flag}: "${value}" is not one of: ${accepted.join(", ")}`,
          2
        );
      }
      return canonical;
    }
    case "number": {
      const number = Number(value);
      if (!Number.isFinite(number) || number < (setting.minimum ?? -Infinity)) {
        throw new InstallRefusal(
          "invalid-value",
          `${flag} must be a number >= ${setting.minimum}, got "${value}"`,
          2
        );
      }
      return value;
    }
    case "list": {
      const items = value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      if (items.length === 0) throw new InstallRefusal("invalid-value", `${flag} is empty`, 2);
      return items.join(",");
    }
    case "path": {
      // A path flag is where somebody pastes a key instead of naming its file.
      // Refused without echoing it: the value may be the secret itself.
      if (value.includes("-----BEGIN") || /[\r\n]/.test(value)) {
        throw new InstallRefusal(
          "key-material-on-command-line",
          `${flag} takes the path to a file, not its contents — never paste key material ` +
            `into a command line`,
          2
        );
      }
      // Absolute, always: the client launches the server from a working
      // directory nobody chose, so a relative path would resolve somewhere else.
      const path = resolve(cwd, value);
      const shown = setting.sensitive ? "(path not shown: it names a private key)" : path;
      if (setting.mustExist) {
        if (!existsSync(path) || !statSync(path).isFile()) {
          throw new InstallRefusal("file-missing", `${flag}: no such file ${shown}`);
        }
      } else if (!existsSync(dirname(path)) || !statSync(dirname(path)).isDirectory()) {
        throw new InstallRefusal(
          "file-missing",
          `${flag}: the directory ${dirname(path)} does not exist, so the server could not ` +
            `create the file`
        );
      }
      return path;
    }
    default:
      return value;
  }
}

/** Build, validate and assess the configuration an install would write.
 *
 * Pure apart from reading the files the flags name: it never opens an OPC UA
 * connection and never touches the client config. Throws `InstallRefusal`.
 */
export function planInstall(
  options: InstallOptions,
  ctx: { env: NodeJS.ProcessEnv; cwd: string }
): InstallPlan {
  const settings = configSchema().settings;
  const values: Record<string, string> = {};
  for (const setting of installerSettings()) {
    const raw = options.settings[setting.key];
    if (raw !== undefined) values[setting.env] = normalise(setting, raw, ctx.cwd);
  }
  // Written even when it is the default: a config that says `observe` says so
  // to whoever reads it next, and cannot be widened by a policy file's profile.
  values.OPCUA_PROFILE ??= "observe";

  // --- the password: from the environment, never from argv ----------------------
  const username = values.OPCUA_USERNAME;
  const envVars: string[] = [];
  if (options.storePasswordInConfig) {
    if (username === undefined) {
      throw new InstallRefusal("invalid-value", "--store-password-in-config needs --username", 2);
    }
    const password = ctx.env[PASSWORD_ENV];
    if (!password) {
      throw new InstallRefusal(
        "password-not-in-environment",
        `--store-password-in-config copies ${PASSWORD_ENV} from this shell's environment, ` +
          `and it is not set`
      );
    }
    values[PASSWORD_ENV] = password;
  } else if (username !== undefined) {
    if (options.client === "codex") {
      // Codex forwards a named variable from its own environment, so the
      // password never has to be written anywhere by us.
      envVars.push(PASSWORD_ENV);
    } else {
      throw new InstallRefusal(
        "password-needs-storage",
        `${clientName(options.client)} has no way to pass a password to a hand-registered ` +
          `server except writing it into its config file. Use the .mcpb bundle, which keeps ` +
          `it in the OS keychain; or X.509 user login (--user-cert, --user-key); or export ` +
          `${PASSWORD_ENV} and pass --store-password-in-config to write it in plain text`
      );
    }
  }

  const env: Record<string, string> = {};
  for (const setting of settings) {
    if (values[setting.env] !== undefined) env[setting.env] = values[setting.env];
  }

  // --- the server's own verdict ---------------------------------------------------
  // The same parsers `runMain` calls at startup, so a config the server would
  // refuse is refused here instead, before it is written. A password Codex will
  // pass through is present at launch, so it stands in for one here.
  const probe: NodeJS.ProcessEnv = { ...env };
  if (envVars.includes(PASSWORD_ENV)) probe[PASSWORD_ENV] = "supplied-at-launch";
  let security: ReturnType<typeof parseSecurityConfig>;
  let policy: ToolPolicy;
  let fileProfile: string | null = null;
  try {
    security = parseSecurityConfig(probe);
    policy = new ToolPolicy(parsePolicyConfig(probe));
    parseReconnectConfig(probe);
    if (env.OPCUA_POLICY_FILE && options.settings.profile === undefined) {
      fileProfile = parsePolicyConfig({ ...probe, OPCUA_PROFILE: "" }).profile;
    }
  } catch (error) {
    throw new InstallRefusal(
      "invalid-config",
      `the server would refuse this configuration at startup: ${(error as Error).message}`
    );
  }

  const { config } = policy;
  const url = env.OPCUA_SERVER_URL;
  const loopback = isLoopbackEndpoint(url);
  const pinned = security.serverCert !== undefined;
  const control = config.profile !== "observe";
  const controlTools = CONTRACT.tools
    .filter(
      (t) =>
        (t.accessClass === "control" || t.accessClass === "alarm-action") && policy.isVisible(t)
    )
    .map((t) => t.name)
    .sort();

  // --- the one refusal that is about safety rather than validity ------------------
  if (control && !loopback && !pinned && !options.allowUnverifiedRemoteControl) {
    throw new InstallRefusal(
      "unverified-remote-control",
      `profile=${config.profile} would give control of ${endpointHost(url) || url}, a remote ` +
        `endpoint whose identity nothing verifies. Pin its certificate with --server-cert ` +
        `(with --security-policy, --client-cert and --client-key), or pass ` +
        `--allow-unverified-remote-control to write it anyway on a lab network`
    );
  }

  const warnings: Finding[] = [];
  const warn = (code: string, message: string) => warnings.push({ code, message });
  if (control && !loopback && !pinned) {
    warn(
      "unverified-remote-control",
      `writing profile=${config.profile} for a remote endpoint whose identity is not ` +
        `verified, because --allow-unverified-remote-control was given`
    );
  }
  if (!loopback && security.policy === "None") {
    warn(
      "no-channel-security",
      "the endpoint is remote and the channel has no security policy: every value read or " +
        "written crosses the network unencrypted, to whoever answers. Set --security-policy"
    );
  } else if (!loopback && !pinned) {
    warn(
      "server-not-pinned",
      "the channel is encrypted to whichever server answers at this address. Pin the " +
        "server's certificate with --server-cert"
    );
  }
  if (security.username !== undefined && security.policy === "None") {
    warn(
      "password-over-unencrypted-channel",
      "the password crosses an unencrypted channel unless the server's user-token policy " +
        "protects it. Set --security-policy"
    );
  }
  if (env[PASSWORD_ENV] !== undefined) {
    warn(
      "password-stored-in-config",
      "the password is written in plain text into the client config file, readable by " +
        "anything that can read that file"
    );
  }
  if (config.allowInsecureControl) {
    warn(
      "insecure-control-override",
      "control tools are allowed over an unsecured channel (OPCUA_ALLOW_INSECURE_CONTROL). " +
        "Never use this against production equipment"
    );
  }
  if (config.allowOutOfRangeWrites) {
    warn(
      "out-of-range-writes",
      "writes outside the node's published EURange are allowed (OPCUA_ALLOW_OUT_OF_RANGE_WRITES)"
    );
  }
  if (config.profile === "full") {
    warn(
      "full-profile",
      "profile=full offers every tool with no allowlist; use it only with a tightly scoped " +
        "OPC UA account"
    );
  }
  if (control && env.OPCUA_AUDIT_FILE === undefined) {
    warn(
      "control-without-audit-file",
      "control calls are audited to stderr only, which the client may not keep. Set --audit-file"
    );
  }
  if (control && controlTools.length === 0) {
    warn(
      "no-control-tools",
      `profile=${config.profile} offers no control tool with these settings: set an ` +
        `allowlist (--allowed-write-nodes, --allowed-methods, --allow-acknowledge-alarms or ` +
        `a --policy-file) and a secured channel`
    );
  }
  if (fileProfile !== null && fileProfile !== config.profile) {
    warn(
      "policy-file-profile-overridden",
      `the policy file asks for profile=${fileProfile}, but a control profile must be chosen ` +
        `with --profile, so this config pins profile=${config.profile}`
    );
  }

  const user =
    security.userCert !== undefined
      ? "X.509 certificate"
      : security.username === undefined
        ? "anonymous"
        : `"${security.username}", ` +
          (envVars.includes(PASSWORD_ENV)
            ? `password from $${PASSWORD_ENV} when the client starts the server`
            : "password stored in the config file");
  const summary = [
    "Security summary:",
    `  endpoint         ${url} (${loopback ? "this machine" : "remote"})`,
    `  channel          ${
      security.policy === "None"
        ? "None: unencrypted and unsigned"
        : `${security.policy}, ${security.mode}`
    }`,
    `  server identity  ${
      security.policy === "None"
        ? "not verified (no channel security)"
        : pinned
          ? "verified: pinned certificate"
          : "NOT verified: no --server-cert"
    }`,
    `  user             ${user}`,
    `  profile          ${config.profile}${config.profile === "observe" ? " (read-only)" : ""}`,
    `  control tools    ${controlTools.length ? controlTools.join(", ") : "none"}`,
    `  policy file      ${env.OPCUA_POLICY_FILE ?? "none"}`,
    `  audit trail      ${env.OPCUA_AUDIT_FILE ?? "stderr only"}`,
  ];

  return { env, envVars, summary, warnings };
}

/** `env` with every sensitive value replaced, for printing. */
export function redactEnv(env: Record<string, string>): Record<string, string> {
  const sensitive = new Set(
    configSchema()
      .settings.filter((s) => s.sensitive || s.secret)
      .map((s) => s.env)
  );
  return Object.fromEntries(
    Object.entries(env).map(([name, value]) => [name, sensitive.has(name) ? REDACTED : value])
  );
}

/** A JSON client config fit to print: ours redacted, other servers' values hidden.
 *
 * Other servers' `env` and `headers` routinely carry API tokens; we cannot tell
 * which, and a preview is not a reason to put any of them on a screen or in a log.
 */
function redactJsonConfig(config: Record<string, any>): Record<string, any> {
  const servers: Record<string, any> = {};
  for (const [name, server] of Object.entries<any>(config.mcpServers ?? {})) {
    if (name === SERVER_KEY) {
      servers[name] = { ...server, env: redactEnv(server.env) };
      continue;
    }
    if (!server || typeof server !== "object" || Array.isArray(server)) {
      servers[name] = server;
      continue;
    }
    const copy: Record<string, any> = { ...server };
    for (const field of ["env", "headers"]) {
      const map = copy[field];
      if (map && typeof map === "object" && !Array.isArray(map)) {
        copy[field] = Object.fromEntries(Object.keys(map).map((k) => [k, REDACTED]));
      }
    }
    servers[name] = copy;
  }
  return { ...config, mcpServers: servers };
}

// --- files ----------------------------------------------------------------------

/** Timestamp suffix for a backup file: sortable, filename-safe, second-resolution. */
function backupSuffix(now: Date = new Date()): string {
  return now.toISOString().replace(/[-:]/g, "").replace(/\..*$/, "");
}

/** A client config file's text, or "" when there is none. */
function readText(path: string): string {
  try {
    return readFileSync(path, "utf8");
  } catch (err: any) {
    if (err?.code === "ENOENT") return "";
    throw err;
  }
}

/** Parse a JSON client config, tolerating absence but not corruption.
 *
 * An unparseable config is a hard error: overwriting it would silently destroy
 * every other MCP server the user has configured.
 */
function parseJsonConfig(path: string, raw: string): unknown {
  if (raw.trim() === "") return {};
  try {
    return JSON.parse(raw);
  } catch (err: any) {
    throw new InstallRefusal(
      "unreadable-config",
      `${path} is not valid JSON (${err.message}) — fix or move it, then retry`
    );
  }
}

/** Write `text` to `path` atomically, backing up anything already there.
 *
 * `secret` makes the file readable by its owner only. Otherwise an existing
 * file keeps its mode: a user who had already locked their config down must not
 * find it loosened by an install.
 */
function writeText(path: string, text: string, secret: boolean): string | null {
  mkdirSync(dirname(path), { recursive: true });

  let mode: number | undefined = secret ? 0o600 : undefined;
  let backup: string | null = null;
  try {
    const existing = statSync(path);
    if (mode === undefined) mode = existing.mode & 0o777;
    backup = `${path}.bak-${backupSuffix()}`;
    copyFileSync(path, backup);
  } catch (err: any) {
    if (err?.code !== "ENOENT") throw err;
    backup = null; // nothing was there to back up
  }

  // Write-then-rename: a crash mid-write must not leave a truncated config that
  // takes every other MCP server down with it.
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, text, { encoding: "utf8", ...(mode === undefined ? {} : { mode }) });
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
    /** Diagnostics: the security summary, warnings and errors. Defaults to `log`. */
    err?: (msg: string) => void;
    /** Overrides the resolved client config path; for tests. */
    configPath?: string;
    /** The installer's own environment; only OPCUA_PASSWORD is ever read from it. */
    env?: NodeJS.ProcessEnv;
    cwd?: string;
  }
): number {
  const err = io.err ?? io.log;
  const path = io.configPath ?? clientConfigPath(options.client);
  const name = clientName(options.client);
  try {
    if (path === null) {
      throw new InstallRefusal(
        "no-config-location",
        `${name} has no known config location on ${process.platform}.`
      );
    }
    const plan = planInstall(options, { env: io.env ?? process.env, cwd: io.cwd ?? process.cwd() });
    const entry = serverEntry({ execPath: io.execPath, scriptPath: io.scriptPath, env: plan.env });
    const redacted: ServerEntry = { ...entry, env: redactEnv(entry.env) };
    if (plan.envVars.length) {
      entry.env_vars = plan.envVars;
      redacted.env_vars = plan.envVars;
    }

    let text: string;
    let preview: string;
    let replaced: boolean;
    const existing = readText(path);
    if (options.client === "codex") {
      ({ text, replaced } = mergeCodexConfig(existing, entry, { force: options.force }));
      preview =
        "# Only this server's tables are shown; the rest of the file is left unchanged.\n" +
        codexBlock(redacted).trimEnd();
    } else {
      const merged = mergeServerEntry(parseJsonConfig(path, existing), entry, {
        force: options.force,
      });
      ({ replaced } = merged);
      text = `${JSON.stringify(merged.config, null, 2)}\n`;
      preview = JSON.stringify(redactJsonConfig(merged.config), null, 2);
    }

    if (options.dryRun) {
      io.log(`Would write ${path}:`);
      io.log(preview);
    } else {
      const backup = writeText(path, text, plan.env[PASSWORD_ENV] !== undefined);
      io.log(`${replaced ? "Replaced" : "Added"} MCP server "${SERVER_KEY}" in ${path}`);
      if (backup) io.log(`Previous config backed up to ${backup}`);
      io.log(`Restart ${name} for the change to take effect.`);
    }
    for (const line of plan.summary) err(line);
    for (const warning of plan.warnings) err(`WARNING [${warning.code}]: ${warning.message}`);
    return 0;
  } catch (error) {
    if (!(error instanceof InstallRefusal)) throw error;
    err(`Error [${error.code}]: ${error.message}`);
    return error.exitCode;
  }
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
    env?: NodeJS.ProcessEnv;
    cwd?: string;
  }
): number {
  switch (action.kind) {
    case "help":
      io.log(usage());
      return 0;
    case "version":
      io.log(VERSION);
      return 0;
    case "error":
      io.err(`opcua-mcp-server: ${action.message}`);
      io.err(usage());
      return 2;
    case "install":
      return runInstall(action.options, io);
    case "verify-audit":
      return runVerify(action.argv, io);
  }
}
