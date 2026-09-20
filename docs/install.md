# Installing

Four ways in, roughly in order of how little you need already installed.

| | You need | Best for |
|---|---|---|
| [MCP bundle (`.mcpb`)](#1-mcp-bundle-mcpb--claude-desktop) | Claude Desktop | Claude Desktop users. Nothing else to install. |
| [Single-file executable](#2-single-file-executable--no-runtime-at-all) | Nothing | Locked-down or air-gapped machines; MCP clients other than Claude Desktop. |
| [`--install claude-desktop`](#3---install-claude-desktop--let-the-server-write-the-config) | Node or Python | You already have a runtime and want the config written for you. |
| [Editing the config by hand](#4-editing-the-config-by-hand) | Node or Python | Scripted rollouts, unusual clients, or auditing exactly what runs. |

> [!WARNING]
> Whichever route you take, the **default** is an unencrypted, anonymous
> connection — fine for the bundled mock or a lab server, not for production
> equipment. Set a security policy and credentials before pointing it at anything
> real: `OPCUA_SECURITY_POLICY`, `OPCUA_SECURITY_MODE`, `OPCUA_CLIENT_CERT`,
> `OPCUA_CLIENT_KEY`, `OPCUA_USERNAME` and `OPCUA_PASSWORD` (the `.mcpb` bundle
> offers all of these as settings fields). Both servers warn on stderr while
> running unsecured.
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

The two are interchangeable — same tools, same behaviour, same version — and are
built from the two runtimes this repo maintains. Take the Python one unless you
have a reason not to; it is a quarter of the size. `<platform>` is `linux`,
`darwin` (macOS) or `win32`, and `<arch>` is `x64` or `arm64`.

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

## 3. `--install claude-desktop` — let the server write the config

If you already have Node or Python, install the package and let it register
itself. This is worth preferring over editing JSON even if you are comfortable
with JSON, because it writes **absolute paths** to the interpreter and the
server. Claude Desktop is launched from the GUI and does not inherit your login
shell's `PATH`, so a config that says `"command": "npx"` frequently works in a
terminal and fails in the app — the single most common way MCP setup goes wrong,
and `nvm` users hit it every time.

```bash
# Node
npm install -g opcua-mcp-server
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840

# Python
uv tool install opcua-mcp-server        # or: pip install opcua-mcp-server
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

Restart Claude Desktop afterwards.

| Flag | Meaning |
|---|---|
| `--install <client>` | Currently `claude-desktop` |
| `--url <endpoint>` | Endpoint to record. Defaults to `$OPCUA_SERVER_URL`, else `opc.tcp://localhost:4840` |
| `--force` | Replace an existing `opcua` entry instead of refusing |
| `--dry-run` | Print the resulting config instead of writing it |
| `--version`, `--help` | As expected |

It merges into whatever is already in the file, leaving your other MCP servers
untouched, and copies the previous config to `claude_desktop_config.json.bak-<timestamp>`
before writing. A config it cannot parse is an error rather than something to
overwrite. Run it with `--dry-run` first if you want to see the result before
committing to it.

The config file it writes lives at:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json`, else `~/.config/Claude/...` |

## 4. Editing the config by hand

The form the [README quick start](../README.md#quick-start) documents, and the
right one for Claude Code (`claude mcp add …`), Cursor, or anything scripted:

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

Either. They are interchangeable — the same tools with the same arguments and
the same responses, held to one shared contract by the test suite — so this is a
question of what the machine already has, not of capability.

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

Two differences that are not cosmetic. The Node runtime implements two extra
security policies (`Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss`) that
`python-opcua` does not, and it sniffs certificate files by content where the
Python runtime goes by extension — so a PEM key must be named `*.pem` there. Both
are covered in [certificates.md](certificates.md).

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
