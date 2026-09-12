# Testing the OPC UA MCP Servers

Three ways to test, from fully automated to fully interactive:

1. [Automated end-to-end suite](#1-automated-end-to-end-suite) (`pytest`)
2. [MCP Inspector](#2-mcp-inspector) — point-and-click or one-line CLI
3. [AI agents](#3-ai-agents) — Claude Code, Claude Desktop, Cursor

Plus [a secured connection by hand](#4-a-secured-connection-by-hand), for when
you are turning encryption and credentials on.

> **All three need the mock OPC UA server running first** — it is the simulated
> device the MCP servers talk to.
>
> ```bash
> uv sync --all-packages            # one-time, from the repo root
> uv run --no-sync opcua-mock-server
> # opc.tcp://localhost:4840/freeopcua/server/  (history enabled on all variables)
> ```
>
> Build the Node server once: `cd packages/server-node && npm install && npm run build`.

A handy node-ID reference and per-tool examples live in [examples.md](examples.md).
Common nodes: Temperature `ns=2;i=3`, PumpEnabled `ns=2;i=12`, ValvePosition
`ns=2;i=13`, SystemMode `ns=2;i=19`, Methods folder `ns=2;i=27`, StartProduction
`ns=2;i=28`.

---

## 1. Automated end-to-end suite

Drives **both** servers over stdio with the official `mcp` client SDK and asserts
on real responses, against an unsecured mock and a secured one. Each mock is
started by its fixture on a free ephemeral port, so several checkouts can run the
suite at the same time.

```bash
uv sync --all-packages         # one-time, from the repo root
cd tests
uv run --no-sync pytest -v
uv run --no-sync pytest -v -k python     # only the Python server
uv run --no-sync pytest -v -k "[node]"   # only the Node server
```

The suite starts its own mocks. To point it at a server you manage instead, set
`OPCUA_SERVER_URL` (or `OPCUA_AGGREGATE_SERVER_URL`). See
[../tests/README.md](../tests/README.md) for the full matrix.

---

## 2. MCP Inspector

[`@modelcontextprotocol/inspector`](https://github.com/modelcontextprotocol/inspector)
is the standard tool for exercising an MCP server by hand.

### UI mode (interactive)

```bash
# Node server
OPCUA_SERVER_URL=opc.tcp://localhost:4840/freeopcua/server/ \
  npx @modelcontextprotocol/inspector node packages/server-node/build/index.js

# Python server
OPCUA_SERVER_URL=opc.tcp://localhost:4840/freeopcua/server/ \
  npx @modelcontextprotocol/inspector uv --directory packages/server-python run opcua-mcp-server
```

It prints a `http://localhost:6274/?...` URL. In the browser:

1. Click **Connect** (status should turn green).
2. Open the **Tools** tab → **List Tools**.
   - Seeing **`read_history_opcua_node`** confirms the server detected history
     support on the mock and exposed the tool.
3. Select a tool, fill the form, click **Run Tool**, read the result pane.

Things to try:

| Tool | Arguments | Expected |
|------|-----------|----------|
| `read_opcua_node` | `node_id` = `ns=2;i=3` | `Node ns=2;i=3 value: 26.x` |
| `get_all_variables` | *(none)* | `Found 22 variables: …` |
| `read_history_opcua_node` | `node_id` = `ns=2;i=3`, `num_values` = `5` | 5 records of `{ value, timestamp, status }`, status `Good`, ISO-8601 UTC timestamps — identical on both servers |
| `read_history_opcua_node` | `node_id` = `ns=2;i=3`, `start_time` = `2026-01-01T00:00:00Z` | records within the window |
| `read_history_opcua_node` | `node_id` = `ns=2;i=3`, `start_time` = `nope` | clear error: *Use ISO 8601…* |
| `write_opcua_node` | `node_id` = `ns=2;i=13`, `value` = `80` | `Successfully wrote 80…` |
| `call_opcua_method` | `object_node_id` = `ns=2;i=27`, `method_node_id` = `ns=2;i=28`, `arguments` = `["60"]` | `…Result: true` (SystemMode → AUTO within ~1s) |
| `subscribe_opcua_node` | `node_id` = `ns=2;i=3`, `publishing_interval` = `500` | one record, `change_count` 0 or 1 |
| `list_subscriptions` | *(none)* | a few seconds later, the same record with `change_count` climbing and `changes` filling |
| `unsubscribe_opcua_node` | `subscription_id` = `sub-1` | `Unsubscribed sub-1 from node ns=2;i=3 after N value changes` |

The **Resources** tab lists one resource, `opcua://subscriptions`. Read it while
a subscription is running and it carries the same records as `list_subscriptions`
— that is the point of it: re-readable live state, no tool call spent.

### CLI mode (scriptable, no browser)

```bash
URL=opc.tcp://localhost:4840/freeopcua/server/
BIN="npx -y @modelcontextprotocol/inspector --cli node packages/server-node/build/index.js -e OPCUA_SERVER_URL=$URL"

# list tools
$BIN --method tools/list

# list resources, and read the subscription buffer
$BIN --method resources/list
$BIN --method resources/read --uri opcua://subscriptions

# read history (note: quote node IDs because ';' is a shell separator)
$BIN --method tools/call --tool-name read_history_opcua_node \
     --tool-arg 'node_id=ns=2;i=3' --tool-arg 'num_values=5'

# call a method
$BIN --method tools/call --tool-name call_opcua_method \
     --tool-arg 'object_node_id=ns=2;i=27' --tool-arg 'method_node_id=ns=2;i=28' \
     --tool-arg 'arguments=["60"]'
```

For the Python server, swap the command for
`uv --directory packages/server-python run opcua-mcp-server`.

---

## 3. AI agents

The end goal: an assistant calls these tools from natural language.

### Claude Code

Register both servers at **project scope** (writes `.mcp.json` in the repo root):

```bash
ROOT=$(pwd)
URL=opc.tcp://localhost:4840/freeopcua/server/
claude mcp add opcua-python -s project -e OPCUA_SERVER_URL=$URL \
  -- uv --directory "$ROOT/packages/server-python" run opcua-mcp-server
claude mcp add opcua-node -s project -e OPCUA_SERVER_URL=$URL \
  -- node "$ROOT/packages/server-node/build/index.js"
```

Then, in a **new** Claude Code session started in this directory:

1. Approve `opcua-python` / `opcua-node` when prompted (project servers require
   one-time approval). You can also manage them with the `/mcp` command.
2. Run `/mcp` to confirm both are **connected** and list their tools.
3. Ask away — example prompts:
   - *"List the OPC UA tools you have available."*
   - *"Read the current temperature from the OPC UA server."*
   - *"Show me the last 5 temperature history readings."* → `read_history_opcua_node`
   - *"Give me a full inventory of all variables on the server."*
   - *"Watch the tank level for the next 30 seconds and tell me what it did."* → `subscribe_opcua_node` / `list_subscriptions`
   - *"Start production at 60 units/hour, check the system mode, then stop it."*
   - *"Use the opcua-node server to read node ns=2;i=4 history between 11:00 and 12:00 UTC today."*

> Both servers expose the same tool names (namespaced `opcua-python` /
> `opcua-node`); name a server in your prompt to target one specifically.

### Claude Desktop

Add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS) and restart the app:

```json
{
  "mcpServers": {
    "opcua-node": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/OPCUA-MCP/packages/server-node/build/index.js"],
      "env": { "OPCUA_SERVER_URL": "opc.tcp://localhost:4840/freeopcua/server/" }
    }
  }
}
```

Or let the server write that block for you, which also saves getting the absolute
paths right:

```bash
cd packages/server-node && npm run build
node build/index.js --install claude-desktop \
  --url opc.tcp://localhost:4840/freeopcua/server/ --dry-run
```

Drop `--dry-run` to write it. See [install.md](install.md) for the full flag set.

### Cursor

Add the same `mcpServers` block to your Cursor MCP settings (Settings → MCP), then
ask Cursor's assistant the prompts above.

---

## 4. A secured connection by hand

The suite covers this automatically (`tests/e2e/test_secure_connection_e2e.py`),
but running it yourself is the quickest way to see what your MCP client will
show — and the closest local rehearsal for pointing a server at real equipment.

```bash
# 1. throwaway certificates (server + client), into a directory of your choice
uv run --no-sync python tests/fixtures/pki.py /tmp/opcua-pki

# 2. the secured mock: Basic256Sha256 only, username operator / hunter2.
#    --check-client-uri adds the ApplicationUri check real servers make; the
#    e2e fixture starts it the same way.
uv run --no-sync python tests/fixtures/secure_opcua_server.py \
  --endpoint opc.tcp://127.0.0.1:4843/mcp/secure \
  --cert /tmp/opcua-pki/server.pem --key /tmp/opcua-pki/server_key.pem \
  --uri urn:opcua-mcp:test-server --check-client-uri
```

Then, in another shell, point either server at it:

```bash
export OPCUA_SERVER_URL=opc.tcp://127.0.0.1:4843/mcp/secure
export OPCUA_SECURITY_POLICY=Basic256Sha256
export OPCUA_CLIENT_CERT=/tmp/opcua-pki/client.pem
export OPCUA_CLIENT_KEY=/tmp/opcua-pki/client_key.pem
export OPCUA_USERNAME=operator OPCUA_PASSWORD=hunter2

npx @modelcontextprotocol/inspector node packages/server-node/build/index.js
# or the Python server:
npx @modelcontextprotocol/inspector uv --directory packages/server-python run opcua-mcp-server
```

No `OPCUA_APPLICATION_URI`: both runtimes announce the `subjectAltName` URI of
the client certificate (`urn:opcua-mcp:test-client` here), and the mock refuses
any other — as equipment that checks does.

The server logs `Connected to OPC UA server (policy=Basic256Sha256
mode=SignAndEncrypt user="operator")` on stderr; `Temperature` is `ns=2;i=2`.
Worth trying deliberately wrong: drop `OPCUA_SECURITY_POLICY` (no endpoint to
fall back to), or change the password (`BadUserAccessDenied`).

**Against real equipment this is not the whole story.** The mock accepts any
client certificate; a real server keeps a trust list and will reject yours until
an operator moves it into the trusted folder — usually after one failed
connection puts it in the rejected folder. Expect to do that first connection by
hand. Generating a certificate that real servers accept, and the trust dance
itself, are in [certificates.md](certificates.md).

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| **List Tools is empty or errors** | Mock server not running → `uv run --no-sync opcua-mock-server` |
| **`read_history_opcua_node` not listed** | Connected to a server without history, or wrong `OPCUA_SERVER_URL` |
| **`read_aggregate_opcua_node` not listed** | Expected — the bundled mock advertises no aggregate functions, so the tool is correctly hidden |
| **`Address already in use` on :4840** | Another mock is on the default port; stop it (`lsof -tiTCP:4840 -sTCP:LISTEN \| xargs kill`) or pass `--endpoint`. The test suite is unaffected — it picks its own port. |
| **Project MCP servers `⏸ Pending approval`** | Normal — approve them in a new `claude` session or via `/mcp` |
| **Server exits at once with `Configuration error: …`** | A security variable is set to a combination OPC UA cannot honour; the message names the variable to fix |
| **`BadUserAccessDenied` / `BadIdentityTokenRejected` on every tool** | `OPCUA_USERNAME` / `OPCUA_PASSWORD` rejected by the server |
| **`BadSecurityChecksFailed`, or the server refuses the session** | The client certificate is not in the OPC UA server's trust list — see [certificates.md](certificates.md) |
| **`BadCertificateUriInvalid`** | The announced ApplicationUri is not the certificate's `subjectAltName` URI; usually a stale `OPCUA_APPLICATION_URI`, which can simply be unset |
| **Values "snap back" after a write** | Expected — the mock republishes sensor/actuator state every ~1s; use command variables/methods for lasting changes |
| **Node value lags after a method call** | The mock propagates method effects via its 1 Hz loop; re-read after ~1s |
| **Works in the terminal, fails in Claude Desktop** | Desktop apps do not inherit a login shell's `PATH`, so a bare `"command": "npx"` or `"node"` cannot be found. Use absolute paths — `--install claude-desktop` writes them for you |
| **macOS refuses to run a downloaded executable** | It is ad-hoc signed, not notarised: `xattr -d com.apple.quarantine <binary>` |
