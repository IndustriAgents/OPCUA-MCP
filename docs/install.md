# Installing

Four ways in, roughly in order of how little you need already installed.

| | You need | Best for |
|---|---|---|
| [MCP bundle (`.mcpb`)](#1-mcp-bundle-mcpb--claude-desktop) | Claude Desktop | Claude Desktop users. Nothing else to install. |
| [Single-file executable](#2-single-file-executable--no-runtime-at-all) | Nothing | Locked-down or air-gapped machines; MCP clients other than Claude Desktop. |
| [`--install`](#3---install--let-the-server-write-the-config) | Node or Python | You already have a runtime and want a checked config written for you (Claude Desktop, Codex). |
| [Editing the config by hand](#4-editing-the-config-by-hand) | Node or Python | Scripted rollouts, unusual clients, or auditing exactly what runs. |

> [!WARNING]
> Whichever route you take, the **default** is an unencrypted, anonymous
> connection — fine for the bundled mock or a lab server, not for production
> equipment. Set a security policy and credentials before pointing it at anything
> real: `OPCUA_SECURITY_POLICY`, `OPCUA_SECURITY_MODE`, `OPCUA_CLIENT_CERT`,
> `OPCUA_CLIENT_KEY`, `OPCUA_SERVER_CERT` to pin the server, and a user identity
> (the `.mcpb` bundle offers all of these as settings fields, and `--install`
> as flags). Both servers warn on stderr while running unsecured.
>
> Note also that this server can **write nodes and call methods**, so scope the
> OPC UA account it logs in as to exactly what you intend the assistant to be
> able to do. See [SECURITY.md](../SECURITY.md).

## 1. MCP bundle (`.mcpb`) — Claude Desktop

An [MCP bundle](https://github.com/modelcontextprotocol/mcpb) is a single file
you drag into Claude Desktop. It carries the server and every dependency, Claude
Desktop supplies the Node runtime, and the OPC UA endpoint appears as a settings
field — no runtime to install, no JSON to edit, nothing fetched from the network
at startup.

1. Download `opcua-mcp-server-<version>.mcpb` from the
   [latest release](https://github.com/IndustriAgents/OPCUA-MCP/releases/latest).
2. In Claude Desktop, open **Settings → Extensions**.
3. Drag the file onto that pane.
4. Set **OPC UA endpoint** to your server's URL, e.g. `opc.tcp://192.168.0.10:4840`.
5. For anything other than a simulator, fill in the security fields below it —
   policy (`Basic256Sha256` unless your server is older), client certificate and
   key, username and password. Left blank, the connection is unencrypted and
   anonymous.

To upgrade, install a newer bundle over the old one. To remove it, use the same
Extensions pane — nothing is left behind elsewhere on the machine.

## 2. Single-file executable — no runtime at all

One binary, no Node, no Python, no `npm install`, no network access required
after the download. This is the route for a plant machine that has neither
runtime and no way to get one.

Download the file matching your platform from the
[latest release](https://github.com/IndustriAgents/OPCUA-MCP/releases/latest):

```
opcua-mcp-server-node-<platform>-<arch>       # ~110 MB
opcua-mcp-server-python-<platform>-<arch>     # ~30 MB
```

They are built from the two runtimes this repo maintains, from the same tag, and
behave the same — same tools, same responses, same version — apart from the
[declared runtime differences](compatibility.md#runtime-differences). The Python
one is a quarter of the size. The Node one runs on the maintained OPC UA client
library and offers the two AES security policies, which makes it the one to take
for a network you do not fully trust (see
[SECURITY.md](../SECURITY.md#cve-2022-25304--unbounded-chunk-reassembly-in-python-opcua)).
`<platform>` is `linux`, `darwin` (macOS) or `win32`, and `<arch>` is `x64` or
`arm64`.

Then make it executable and register it:

```bash
chmod +x opcua-mcp-server-python-linux-x64
./opcua-mcp-server-python-linux-x64 --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

**macOS** binaries are ad-hoc signed, not notarised, so Gatekeeper will refuse
the first launch. Clear the quarantine flag once:

```bash
xattr -d com.apple.quarantine opcua-mcp-server-python-darwin-arm64
```

**Windows** SmartScreen will warn for the same reason: choose *More info → Run
anyway*.

## 3. `--install` — let the server write the config

If you already have Node or Python, install the package and let it register
itself with **Claude Desktop** (`--install claude-desktop`) or **Codex**
(`--install codex`). This is worth preferring over editing a config file even if
you are comfortable with one:

- It writes **absolute paths** to the interpreter and the server. Claude Desktop
  is launched from the GUI and does not inherit your login shell's `PATH`, so a
  config that says `"command": "npx"` frequently works in a terminal and fails in
  the app — the single most common way MCP setup goes wrong, and `nvm` users hit
  it every time.
- It can express the whole security model — channel security, a pinned server
  certificate, user identity, tool profile, policy file, audit trail — and it
  checks the result with **the server's own startup parsers** before writing
  anything, so it cannot write a configuration the server would refuse.
- It **fails closed**. The default profile is `observe` (read-only), written into
  the config explicitly. A control profile has to be asked for with `--profile`,
  and a control profile for a remote endpoint whose certificate is not pinned is
  refused (see [Safety rules](#safety-rules)).

The easy path, for a real device — read-only, encrypted, and talking only to the
server whose certificate you pinned:

```bash
# Node
npm install -g opcua-mcp-server
# Python
uv tool install opcua-mcp-server        # or: pip install opcua-mcp-server

opcua-mcp-server --install claude-desktop --dry-run \
  --url opc.tcp://192.168.0.10:4840 \
  --security-policy Basic256Sha256 \
  --client-cert /etc/opcua/client.pem --client-key /etc/opcua/client_key.pem \
  --server-cert /etc/opcua/server.pem
```

`--dry-run` validates everything — that each file exists, that the combination
is one the server accepts — and prints the exact file it would write, a
**redacted** preview of the result and a security summary, without opening an
OPC UA connection or writing anything. Drop `--dry-run` to write it, then
restart the client. Generating those certificates is covered in
[certificates.md](certificates.md).

Against the bundled mock or a lab server on this machine,
`opcua-mcp-server --install claude-desktop` alone is enough.

### Letting the assistant operate equipment

Control tools (writes, method calls, alarm actions) need an explicit profile, an
allowlist, and — for a remote endpoint — a pinned server certificate:

```bash
opcua-mcp-server --install claude-desktop \
  --url opc.tcp://192.168.0.10:4840 \
  --security-policy Basic256Sha256 \
  --client-cert /etc/opcua/client.pem --client-key /etc/opcua/client_key.pem \
  --server-cert /etc/opcua/server.pem \
  --profile operator \
  --policy-file /etc/opcua/policy.json \
  --audit-file /var/log/opcua-mcp/audit.jsonl \
  --operator-id line-1
```

The policy file format, and why each of these matters, is in
[SECURITY.md](../SECURITY.md#tool-profiles-and-control-policy).

### Flags

The setting flags are generated from
[`contract/config.json`](../contract/config.json), the one definition of every
`OPCUA_*` variable both servers read: each is the setting's key with dashes, and
`--help` lists them. Values are checked against the same schema — choices
(case-insensitive, canonicalised), numeric minimums, file paths (made absolute,
because the client starts the server from a directory nobody chose). A boolean
setting flag takes no value: its presence means `true`.

Two settings have no flag, and naming one is refused with a pointer here:
`OPCUA_PASSWORD` (see [Passwords](#passwords)) and `OPCUA_AUDIT_CHAIN_KEY_FILE`,
the HMAC key for `--audit-chain hmac-sha256`, which the schema keeps off command
lines as it does a secret. For an HMAC chain, install with `--audit-chain sha256`
(or none) and set both variables in the client config by hand, or use the
`.mcpb` bundle.

| Flag | Variable |
|---|---|
| `--server-url` (or `--url`) | `OPCUA_SERVER_URL`. Defaults to `$OPCUA_SERVER_URL`, else `opc.tcp://localhost:4840` |
| `--security-policy`, `--security-mode` | `OPCUA_SECURITY_POLICY`, `OPCUA_SECURITY_MODE` |
| `--client-cert`, `--client-key`, `--application-uri` | `OPCUA_CLIENT_CERT`, `OPCUA_CLIENT_KEY`, `OPCUA_APPLICATION_URI` |
| `--server-cert` | `OPCUA_SERVER_CERT`, which pins the server |
| `--username`, `--user-cert`, `--user-key` | `OPCUA_USERNAME`, `OPCUA_USER_CERT`, `OPCUA_USER_KEY` (for the password, see [Passwords](#passwords)) |
| `--profile` | `OPCUA_PROFILE`: `observe` (also `read-only`; the default), `operator`, `full` |
| `--policy-file`, `--allowed-tools`, `--allowed-write-nodes`, `--allowed-methods` | `OPCUA_POLICY_FILE`, `OPCUA_ALLOWED_TOOLS`, `OPCUA_ALLOWED_WRITE_NODES`, `OPCUA_ALLOWED_METHODS` |
| `--allow-acknowledge-alarms`, `--allow-insecure-control`, `--allow-unverified-server-control`, `--allow-out-of-range-writes` | the matching `OPCUA_ALLOW_*` overrides |
| `--audit-file`, `--audit-fsync`, `--audit-chain`, `--operator-id` | `OPCUA_AUDIT_FILE`, `OPCUA_AUDIT_FSYNC`, `OPCUA_AUDIT_CHAIN`, `OPCUA_OPERATOR_ID` |
| `--reconnect-initial-delay-ms`, `--reconnect-max-delay-ms`, `--reconnect-max-retry`, `--session-timeout-ms` | the reconnection settings |

And the installer's own:

| Flag | Meaning |
|---|---|
| `--install <client>` | `claude-desktop` or `codex` |
| `--dry-run` | Validate, print the target file, a redacted preview and the security summary; write nothing |
| `--force` | Replace an existing `opcua` entry instead of refusing |
| `--store-password-in-config` | Copy `$OPCUA_PASSWORD` into the config file, in plain text. See [Passwords](#passwords) |
| `--allow-unverified-remote-control` | Write a control profile for a remote endpoint with no pinned certificate. Lab networks only |
| `--version`, `--help` | As expected |

The two runtimes' installers take the same flags and write the same
configuration for the same input: one shared table of cases,
[`tests/fixtures/install-cases.json`](../tests/fixtures/install-cases.json), holds
both to it. The one exception is a declared runtime difference. Each validates
with its own server's parser, so the Python installer refuses the two AES
security policies its runtime cannot negotiate.

### Safety rules

Refused outright (exit 1, nothing written):

- **A configuration the server would refuse at startup**: a mode without a
  policy, a policy without a client certificate, a pinned server certificate or
  a user certificate with no channel security, a username with no password, an
  unreadable policy file, an unknown tool name, a hash chain with no audit file.
  The installer runs the server's own parsers on what it is about to write.
- **A file that does not exist**, or an audit file in a directory that does not.
- **`--profile operator` or `full` for a remote endpoint without
  `--server-cert`**, unless `--allow-unverified-remote-control` is given. An
  encrypted channel to an unpinned server is encrypted to whoever answers at that
  address. "Remote" is anything but `localhost`, `127.0.0.0/8` and `::1`; an
  address the installer cannot parse counts as remote. The server's own lab
  override, `--allow-unverified-server-control`, does not stand in for this one:
  it is written into the config and opens control at runtime, while
  `--allow-unverified-remote-control` is only the acknowledgement that doing so
  against a remote host is intended.

Refused as usage errors (exit 2): an unknown flag or choice, a number below the
schema's minimum, a blank value, key material pasted into a path flag, and any
secret as a flag (`--password`), or a setting kept off the installer
(`--audit-chain-key-file`).

Written, with a `WARNING [code]` on stderr: a remote endpoint with no channel
security (`no-channel-security`) or with an unpinned server (`server-not-pinned`);
a password on an unencrypted channel; a password stored in the file; the
`OPCUA_ALLOW_INSECURE_CONTROL`, `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` or
`OPCUA_ALLOW_OUT_OF_RANGE_WRITES` overrides; the `full` profile; a control profile
with no audit file, or one the server would offer no control tool (the warning
gives the server's own reason: no allowlist, no security policy, or no pinned
certificate); and a policy file whose `profile` the explicit `observe` default
overrides.

### Passwords

A password is never accepted as a flag: anything on a command line is visible to
every local user and lands in shell history, so `--password` is refused and its
value is never echoed. Private keys are passed as file paths, and a key pasted
into a path flag is refused the same way. Nothing the installer prints (preview,
summary, warnings, errors) contains a secret: `OPCUA_PASSWORD` and the
private-key paths are shown as `<redacted>`, and so is every `env` and `headers`
value of the other MCP servers in the file.

How the password reaches the server depends on the client:

- **Codex** passes named variables through from its own environment, so
  `--install codex --username <name>` writes `env_vars = ["OPCUA_PASSWORD"]` and
  no password at all. Export `OPCUA_PASSWORD` in the environment Codex runs in.
- **Claude Desktop** has no way to hand a password to a server registered in its
  config file except writing it there. The recommended routes are the
  [`.mcpb` bundle](#1-mcp-bundle-mcpb--claude-desktop), which keeps the password
  in the operating system's keychain, or X.509 user login
  (`--user-cert`, `--user-key`), which needs no password. `--install
  claude-desktop --username <name>` on its own is therefore refused.
- To write it anyway, opt in explicitly. The password is read from the
  installer's environment, never from its arguments, and the file is written
  readable by its owner only:

  ```bash
  read -rs OPCUA_PASSWORD && export OPCUA_PASSWORD
  opcua-mcp-server --install claude-desktop --username mcp-operator \
    --store-password-in-config ...
  unset OPCUA_PASSWORD
  ```

### Where it writes

It merges into whatever is already in the file, leaving your other MCP servers
untouched, and copies the previous config to `<file>.bak-<timestamp>` before
writing. A config it cannot parse is an error rather than something to overwrite.
For Codex it replaces only its own `[mcp_servers.opcua]` tables, and refuses an
entry written as dotted keys or an inline table rather than guess at it.

| Client | OS | Path |
|---|---|---|
| Claude Desktop | macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop | Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Claude Desktop | Linux | `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json`, else `~/.config/Claude/...` |
| Codex | all | `$CODEX_HOME/config.toml`, else `~/.codex/config.toml` |

## 4. Editing the config by hand

The form the [README](../README.md#connect-your-agent) documents, and the
right one for Cursor, Gemini CLI, Antigravity, Windsurf, or anything scripted:

```json
{
  "mcpServers": {
    "opcua": {
      "command": "npx",
      "args": ["-y", "opcua-mcp-server"],
      "env": { "OPCUA_SERVER_URL": "opc.tcp://192.168.0.10:4840" }
    }
  }
}
```

If Claude Desktop reports the server failing to start, replace `"npx"` with the
absolute path to your `node` and give it the absolute path to the server — that
is exactly what `--install` does, and why.

## Which runtime am I installing?

Either. Both are first-class ([ADR 0001](adr/0001-two-first-class-runtimes.md)):
one shared contract, one test suite run against both, one release gate, one
version number — the same tools with the same arguments and the same responses.
So this is mostly a question of what the machine already has. Where the two do
differ by design, the difference is declared, and the full list is in
[compatibility.md](compatibility.md#runtime-differences) — along with the known
divergences that are bugs still being fixed.

| | Python | Node |
|---|---|---|
| Requires | Python 3.10+ | Node 22.13+ |
| Package | [PyPI `opcua-mcp-server`](https://pypi.org/project/opcua-mcp-server/) | [npm `opcua-mcp-server`](https://www.npmjs.com/package/opcua-mcp-server) |
| Fetch on demand | `uvx opcua-mcp-server` | `npx -y opcua-mcp-server` |
| Install permanently | `uv tool install opcua-mcp-server` | `npm install -g opcua-mcp-server` |
| MCP framework | `mcp` (`MCPServer`) | `@modelcontextprotocol/sdk` |
| OPC UA library | `opcua` (FreeOpcUa) | `node-opcua-client` |
| Source | `packages/server-python/` | `packages/server-node/` |

Exact dependency versions live in the manifests
([`pyproject.toml`](../packages/server-python/pyproject.toml),
[`package.json`](../packages/server-node/package.json)) rather than being
restated here, where they would drift.

The declared differences most likely to matter when choosing. The Node runtime
implements two extra security policies (`Aes128_Sha256_RsaOaep`,
`Aes256_Sha256_RsaPss`) that `python-opcua` does not, and it sniffs certificate
files by content where the Python runtime goes by extension — so a PEM key must be
named `*.pem` there; both are covered in [certificates.md](certificates.md). The
`.mcpb` bundle and the MCP Registry listing are Node only. And the Python
runtime's OPC UA library is unmaintained — this project patches it
(CVE-2022-25304) and is moving it to `asyncua`
([#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144)).

## Building the artifacts yourself

None of the downloads are required; each is one command from a checkout.

```bash
cd packages/server-node
npm ci
npm run build:mcpb            # -> dist/opcua-mcp-server-<version>.mcpb
npm run build:sea             # -> dist/opcua-mcp-server-node-<platform>-<arch>
```

```bash
uv sync --all-packages --group packaging
uv run --group packaging pyinstaller --noconfirm \
    --distpath packages/server-python/dist \
    --workpath packages/server-python/build-pyinstaller \
    packages/server-python/packaging/opcua-mcp-server.spec
```

Build with a Node that satisfies the server's own floor (>=22.13): each
executable embeds the interpreter it was built with, and then has to run the
server on it. That also means neither can be cross-compiled, so a release builds
one per operating system in
[`.github/workflows/release.yml`](../.github/workflows/release.yml).

`tests/smoke/` builds all of these and drives them against a live OPC UA server;
see [testing.md](testing.md).
