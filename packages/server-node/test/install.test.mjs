// Unit tests for `--install` — argument parsing, the config entry it writes, and
// the merge into an existing client config. No filesystem beyond a temp dir, no
// OPC UA server, no MCP transport.
//
// The Python equivalents are in tests/unit/test_install.py, and
// tests/unit/test_install_parity.py checks the two runtimes actually produce the
// same entry when run.
//
// Run: npm test   (requires `npm run build` first — these import build/install.js)
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { describe } from "node:test";

import {
  CLIENTS,
  SERVER_KEY,
  claudeDesktopConfigPath,
  codexBlock,
  codexConfigPath,
  isEphemeralInstall,
  isLoopbackEndpoint,
  mergeCodexConfig,
  mergeServerEntry,
  parseArgs,
  runInstall,
  serverEntry,
  tomlString,
} from "../build/install.js";

const URL_ = "opc.tcp://plc:4840";
const ENV = { OPCUA_SERVER_URL: URL_ };
const scratch = () => mkdtempSync(join(tmpdir(), "opcua-install-"));

describe("claudeDesktopConfigPath", () => {
  test("macOS uses Application Support", () => {
    // Built with `join` rather than written out, because that is what the
    // function uses and `join` spells a separator the host's way: asserting a
    // POSIX literal made this a test that passed on macOS and Linux and failed
    // on Windows for saying nothing about the product. What is under test is the
    // segments, and they are the same everywhere.
    assert.equal(
      claudeDesktopConfigPath("darwin", {}, "/Users/e"),
      join("/Users/e", "Library", "Application Support", "Claude", "claude_desktop_config.json")
    );
  });

  test("Windows prefers APPDATA over a guessed profile path", () => {
    assert.equal(
      claudeDesktopConfigPath("win32", { APPDATA: "/R" }, "/U/e"),
      join("/R", "Claude", "claude_desktop_config.json")
    );
    assert.equal(
      claudeDesktopConfigPath("win32", {}, "/U/e"),
      join("/U/e", "AppData", "Roaming", "Claude", "claude_desktop_config.json")
    );
  });

  test("Linux honours XDG_CONFIG_HOME", () => {
    assert.equal(
      claudeDesktopConfigPath("linux", { XDG_CONFIG_HOME: "/x" }, "/home/e"),
      join("/x", "Claude", "claude_desktop_config.json")
    );
    assert.equal(
      claudeDesktopConfigPath("linux", {}, "/home/e"),
      join("/home/e", ".config", "Claude", "claude_desktop_config.json")
    );
  });

  // Better to say "no known location on aix" than to write a file nobody reads.
  test("an unsupported platform has no path rather than a wrong one", () => {
    assert.equal(claudeDesktopConfigPath("aix", {}, "/home/e"), null);
  });
});

describe("isEphemeralInstall", () => {
  test("recognises npx and dlx caches", () => {
    assert.equal(isEphemeralInstall("/home/e/.npm/_npx/abc/node_modules/x/build/index.js"), true);
    assert.equal(isEphemeralInstall("/home/e/.local/share/pnpm/dlx-1234/node_modules/x.js"), true);
  });

  test("recognises them spelled the Windows way too", () => {
    // Both separators, on every host. A path here comes from `process.argv[1]`
    // or from a caller rather than from `path.join`, and Windows accepts either
    // — so a forward-slashed Windows path used to be one unsplit segment that
    // matched nothing, and a cache path was written into the config as though it
    // were a real install.
    const windows = "C:\\Users\\e\\AppData\\Local\\npm-cache\\_npx\\abc\\node_modules\\x.js";
    assert.equal(isEphemeralInstall(windows), true);
    assert.equal(isEphemeralInstall("C:/Users/e/AppData/Local/npm-cache/_npx/abc/x.js"), true);
    assert.equal(
      isEphemeralInstall("C:\\Program Files\\nodejs\\node_modules\\opcua-mcp-server\\x.js"),
      false
    );
  });

  test("a global or local install is not ephemeral", () => {
    const global_ = "/usr/local/lib/node_modules/opcua-mcp-server/build/index.js";
    assert.equal(isEphemeralInstall(global_), false);
    assert.equal(
      isEphemeralInstall("/srv/app/node_modules/opcua-mcp-server/build/index.js"),
      false
    );
  });
});

describe("serverEntry", () => {
  // Why absolute rather than `"command": "node"`: Claude Desktop is launched from
  // the GUI and does not inherit a login shell's PATH, so a bare interpreter name
  // is the classic "works in my terminal, not in the app" failure.
  test("a real install names the interpreter and the script absolutely", () => {
    const entry = serverEntry({ execPath: "/usr/bin/node", scriptPath: "/o/index.js", env: ENV });
    assert.deepEqual(entry, {
      command: "/usr/bin/node",
      args: ["/o/index.js"],
      env: { OPCUA_SERVER_URL: URL_ },
    });
  });

  test("a single-file build names only itself", () => {
    const entry = serverEntry({ execPath: "/opt/opcua-mcp-server", scriptPath: null, env: ENV });
    assert.deepEqual(entry, {
      command: "/opt/opcua-mcp-server",
      env: { OPCUA_SERVER_URL: URL_ },
    });
  });

  // An npx cache path is correct today and gone tomorrow, so record the command
  // that re-fetches the package instead.
  test("an npx run records the npx invocation, not the cache path", () => {
    assert.deepEqual(
      serverEntry({ execPath: "/usr/bin/node", scriptPath: "/h/.npm/_npx/a/i.js", env: ENV }),
      { command: "npx", args: ["-y", "opcua-mcp-server"], env: { OPCUA_SERVER_URL: URL_ } }
    );
  });
});

describe("mergeServerEntry", () => {
  const entry = { command: "/n", args: ["/i.js"], env: { OPCUA_SERVER_URL: URL_ } };

  test("adds to an empty config", () => {
    const { config, replaced } = mergeServerEntry({}, entry);
    assert.equal(replaced, false);
    assert.deepEqual(config.mcpServers[SERVER_KEY], entry);
  });

  // The user's other MCP servers are not ours to touch; this is the whole risk
  // of writing to a config file we did not create.
  test("leaves other servers and unrelated top-level keys alone", () => {
    const existing = { theme: "dark", mcpServers: { filesystem: { command: "fs" } } };
    const { config } = mergeServerEntry(existing, entry);
    assert.equal(config.theme, "dark");
    assert.deepEqual(config.mcpServers.filesystem, { command: "fs" });
  });

  test("refuses to replace an existing entry without --force", () => {
    const existing = { mcpServers: { [SERVER_KEY]: { command: "old" } } };
    assert.throws(() => mergeServerEntry(existing, entry), /already configured/);
    const { config, replaced } = mergeServerEntry(existing, entry, { force: true });
    assert.equal(replaced, true);
    assert.deepEqual(config.mcpServers[SERVER_KEY], entry);
  });

  test("does not mutate the config it was given", () => {
    const existing = { mcpServers: { filesystem: { command: "fs" } } };
    mergeServerEntry(existing, entry);
    assert.deepEqual(Object.keys(existing.mcpServers), ["filesystem"]);
  });

  test("rejects an mcpServers that is not an object rather than discarding it", () => {
    assert.throws(() => mergeServerEntry({ mcpServers: [] }, entry), /not an object/);
  });
});

describe("parseArgs", () => {
  // The no-argument case is how every MCP client starts us; it must never be
  // interpreted as a CLI request.
  test("no arguments means serve", () => {
    assert.deepEqual(parseArgs([]), { kind: "serve" });
  });

  test("--help and --version short-circuit", () => {
    assert.equal(parseArgs(["--help"]).kind, "help");
    assert.equal(parseArgs(["-h"]).kind, "help");
    assert.equal(parseArgs(["--version"]).kind, "version");
    assert.equal(parseArgs(["-v"]).kind, "version");
  });

  test("--install collects the client, url and flags", () => {
    const action = parseArgs(
      ["--install", "claude-desktop", "--url", URL_, "--force", "--dry-run"],
      "opc.tcp://default:4840"
    );
    assert.deepEqual(action, {
      kind: "install",
      options: {
        client: "claude-desktop",
        settings: { server_url: URL_ },
        force: true,
        dryRun: true,
        storePasswordInConfig: false,
        allowUnverifiedRemoteControl: false,
      },
    });
  });

  test("--url defaults to the configured endpoint", () => {
    const action = parseArgs(["--install", "claude-desktop"], "opc.tcp://default:4840");
    assert.equal(action.options.settings.server_url, "opc.tcp://default:4840");
  });

  test("unknown clients, unknown flags and missing values are errors", () => {
    assert.equal(parseArgs(["--install", "emacs"]).kind, "error");
    assert.equal(parseArgs(["--frobnicate"]).kind, "error");
    assert.equal(parseArgs(["--install"]).kind, "error");
    assert.equal(parseArgs(["--url"]).kind, "error");
    assert.equal(parseArgs(["--force"]).kind, "error"); // --install is required
  });

  // Was a real hazard: `--url` swallowed the next token whatever it was, so a
  // forgotten endpoint turned `--dry-run` into the URL *and* consumed the flag
  // that was meant to prevent any write. The result was a real config written
  // with a nonsense endpoint, reported as success.
  test("a flag is never accepted as the value of another flag", () => {
    const action = parseArgs(["--install", "claude-desktop", "--url", "--dry-run"]);
    assert.equal(action.kind, "error");
    assert.match(action.message, /--url needs an endpoint/);
  });

  test("a missing client name is an error, not the next flag", () => {
    const action = parseArgs(["--install", "--dry-run"]);
    assert.equal(action.kind, "error");
    assert.match(action.message, /--install needs a client name/);
  });

  // argparse accepts `--url=value`, so the Node runtime has to as well; a user
  // following the Python docs must not be told it is an unknown argument.
  test("--flag=value is accepted, like argparse", () => {
    const action = parseArgs(["--install=claude-desktop", `--url=${URL_}`, "--dry-run"]);
    assert.equal(action.options.client, "claude-desktop");
    assert.equal(action.options.settings.server_url, URL_);
    assert.equal(action.options.force, false);
    assert.equal(action.options.dryRun, true);
  });

  test("an endpoint containing '=' survives the split", () => {
    const url = "opc.tcp://h:4840/path?a=b";
    assert.equal(
      parseArgs(["--install=claude-desktop", `--url=${url}`]).options.settings.server_url,
      url
    );
  });

  // argparse: "ignored explicit argument" — a hard error, not a silent accept.
  test("a value attached to a boolean flag is rejected", () => {
    assert.equal(parseArgs(["--install=claude-desktop", "--force=yes"]).kind, "error");
    assert.equal(parseArgs(["--install=claude-desktop", "--dry-run=1"]).kind, "error");
  });

  // An endpoint is not an option token, so the guard above must not reject one.
  test("flags after a consumed value are still parsed", () => {
    const action = parseArgs(["--install", "claude-desktop", "--url", URL_, "--dry-run"]);
    assert.equal(action.options.client, "claude-desktop");
    assert.equal(action.options.settings.server_url, URL_);
    assert.equal(action.options.force, false);
    assert.equal(action.options.dryRun, true);
  });

  test("every advertised client is accepted", () => {
    for (const client of CLIENTS) {
      assert.equal(parseArgs(["--install", client]).kind, "install");
    }
  });
});

describe("runInstall", () => {
  const io = (dir, log = []) => ({
    execPath: "/usr/bin/node",
    scriptPath: "/opt/opcua/index.js",
    configPath: join(dir, "claude_desktop_config.json"),
    log: (msg) => log.push(msg),
    env: {},
    cwd: dir,
  });

  const options = {
    client: "claude-desktop",
    settings: { server_url: URL_ },
    force: false,
    dryRun: false,
    storePasswordInConfig: false,
    allowUnverifiedRemoteControl: false,
  };

  test("creates the config file and its parent directory", () => {
    const dir = join(scratch(), "Claude"); // deliberately absent
    const target = join(dir, "claude_desktop_config.json");
    assert.equal(runInstall(options, { ...io(dir), configPath: target }), 0);
    assert.deepEqual(JSON.parse(readFileSync(target, "utf8")).mcpServers[SERVER_KEY], {
      command: "/usr/bin/node",
      args: ["/opt/opcua/index.js"],
      env: { OPCUA_SERVER_URL: URL_, OPCUA_PROFILE: "observe" },
    });
  });

  test("--dry-run prints the result and writes nothing", () => {
    const dir = scratch();
    const log = [];
    assert.equal(runInstall({ ...options, dryRun: true }, io(dir, log)), 0);
    assert.deepEqual(readdirSync(dir), []);
    assert.match(log.join("\n"), /"OPCUA_SERVER_URL": "opc.tcp:\/\/plc:4840"/);
  });

  test("backs up an existing config before replacing an entry", () => {
    const dir = scratch();
    const target = join(dir, "claude_desktop_config.json");
    const before = { mcpServers: { [SERVER_KEY]: { command: "old" }, other: { command: "x" } } };
    writeFileSync(target, JSON.stringify(before));

    assert.equal(runInstall({ ...options, force: true }, io(dir)), 0);

    const backups = readdirSync(dir).filter((f) => f.includes(".bak-"));
    assert.equal(backups.length, 1, "expected exactly one backup");
    assert.deepEqual(JSON.parse(readFileSync(join(dir, backups[0]), "utf8")), before);
    const after = JSON.parse(readFileSync(target, "utf8"));
    assert.equal(after.mcpServers[SERVER_KEY].command, "/usr/bin/node");
    assert.deepEqual(after.mcpServers.other, { command: "x" });
  });

  test("an existing entry without --force fails without touching the file", () => {
    const dir = scratch();
    const target = join(dir, "claude_desktop_config.json");
    const before = JSON.stringify({ mcpServers: { [SERVER_KEY]: { command: "old" } } });
    writeFileSync(target, before);

    const log = [];
    assert.equal(runInstall(options, io(dir, log)), 1);
    assert.equal(readFileSync(target, "utf8"), before);
    assert.match(log.join("\n"), /already configured/);
  });

  // Overwriting an unreadable config would silently destroy every other MCP
  // server the user has set up, so it has to be a hard stop.
  test("a corrupt config is an error, not something to overwrite", () => {
    const dir = scratch();
    const target = join(dir, "claude_desktop_config.json");
    writeFileSync(target, "{ not json");

    const log = [];
    assert.equal(runInstall(options, io(dir, log)), 1);
    assert.equal(readFileSync(target, "utf8"), "{ not json");
    assert.match(log.join("\n"), /not valid JSON/);
  });

  test("an empty config file is treated as an empty config", () => {
    const dir = scratch();
    const target = join(dir, "claude_desktop_config.json");
    writeFileSync(target, "");
    assert.equal(runInstall(options, io(dir)), 0);
    assert.ok(JSON.parse(readFileSync(target, "utf8")).mcpServers[SERVER_KEY]);
  });
});

// --- Codex (#135). The Python equivalents are in tests/unit/test_install.py. ------

describe("Codex config.toml", () => {
  const entry = { command: "/p", args: ["/i.js"], env: ENV };

  test("lives under CODEX_HOME, else the home directory", () => {
    assert.equal(codexConfigPath({}, "/h"), join("/h", ".codex", "config.toml"));
    assert.equal(codexConfigPath({ CODEX_HOME: "/x" }, "/h"), join("/x", "config.toml"));
  });

  // Windows paths are full of backslashes; a raw one is an invalid TOML escape.
  test("strings escape what TOML requires", () => {
    assert.equal(tomlString('C:\\Users\\"q"'), String.raw`"C:\\Users\\\"q\""`);
    assert.equal(tomlString("a\nb\x7f"), String.raw`"a\u000ab\u007f"`);
    assert.equal(tomlString("café"), '"café"');
  });

  test("a merge appends and leaves the rest of the file alone", () => {
    const existing = '# mine\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n\n';
    const { text, replaced } = mergeCodexConfig(existing, entry);
    assert.equal(replaced, false);
    assert.ok(text.startsWith('# mine\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n'));
    assert.ok(text.endsWith(codexBlock(entry)));
  });

  test("a merge replaces only its own tables, and only with --force", () => {
    const existing =
      '[mcp_servers."opcua"]\ncommand = "old"\n\n[mcp_servers.opcua.env]\nA = "1"\n\n' +
      '[mcp_servers.other]\ncommand = "x"\n';
    assert.throws(() => mergeCodexConfig(existing, entry), /already configured/);
    const { text, replaced } = mergeCodexConfig(existing, entry, { force: true });
    assert.equal(replaced, true);
    assert.ok(!text.includes("old") && !text.includes('A = "1"'));
    assert.ok(text.includes('[mcp_servers.other]\ncommand = "x"'));
    assert.equal(text.split("[mcp_servers.opcua]").length, 2);
  });

  // Rewriting dotted keys or an inline table needs a real TOML parser.
  test("a merge refuses an entry it cannot rewrite", () => {
    for (const existing of [
      '[mcp_servers]\nopcua = { command = "old" }\n',
      'mcp_servers.opcua.command = "old"\n',
    ]) {
      assert.throws(() => mergeCodexConfig(existing, entry, { force: true }), /does not rewrite/);
    }
  });
});

describe("isLoopbackEndpoint", () => {
  const cases = [
    ["opc.tcp://localhost:4840", true],
    ["opc.tcp://LOCALHOST:4840/path", true],
    ["opc.tcp://127.0.0.1:4840", true],
    ["opc.tcp://127.8.9.10", true],
    ["opc.tcp://[::1]:4840", true],
    ["opc.tcp://user@localhost:4840", true],
    ["opc.tcp://plc:4840", false],
    ["opc.tcp://localhost.example.com:4840", false],
    ["opc.tcp://127.0.0.1.example.com:4840", false],
    ["opc.tcp://10.0.0.1:4840", false],
    ["opc.tcp://[fe80::1]:4840", false],
    ["localhost:4840", false], // no scheme: unparseable, so treated as remote
  ];
  for (const [url, loopback] of cases) {
    test(`${url} is ${loopback ? "this machine" : "remote"}`, () => {
      assert.equal(isLoopbackEndpoint(url), loopback);
    });
  }
});

describe("parseArgs setting flags", () => {
  test("a negative number is a value, as argparse reads it", () => {
    const action = parseArgs(["--install", "codex", "--reconnect-max-retry", "-1"]);
    assert.equal(action.options.settings.reconnect_max_retry, "-1");
  });

  test("a secret flag is refused without echoing its value", () => {
    const action = parseArgs(["--install", "codex", "--password", "hunter2"]);
    assert.equal(action.kind, "error");
    assert.ok(!action.message.includes("hunter2"));
  });
});
