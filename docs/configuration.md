# Configuration

Everything is set through environment variables, which both runtimes read the
same way. In an MCP client config they go in the server entry's `env` block; in
the Claude Desktop bundle they are fields in the settings form.

Most deployments need only a few of them:

| Goal | Set |
|---|---|
| Point at a server | `OPCUA_SERVER_URL` |
| Encrypt and verify the channel | `OPCUA_SECURITY_POLICY`, `OPCUA_CLIENT_CERT`, `OPCUA_CLIENT_KEY`, `OPCUA_SERVER_CERT` — [below](#connecting-securely) |
| Log in as a user | `OPCUA_USERNAME` + `OPCUA_PASSWORD`, or `OPCUA_USER_CERT` + `OPCUA_USER_KEY` |
| Let the agent write or call methods | `OPCUA_PROFILE=operator` plus an allowlist — [below](#deciding-what-the-agent-may-do) |
| Keep a record of control calls | `OPCUA_AUDIT_FILE` |

## One process, one endpoint

`OPCUA_SERVER_URL` is read once at
startup, every tool targets it, and the only transport is stdio — so the server
runs beside the MCP client that started it, and each client gets its own OPC UA
session. That is the right shape for an engineer at a workstation, which is what
this is built for. A site with five PLCs runs five entries in the client config,
and if OPC UA sessions are a licensed resource on your equipment, count on one
per client per endpoint. What it would take to be a shared plant-wide gateway
instead is set out in the [roadmap](../ROADMAP.md#considered-and-set-aside).

## Every setting

The canonical, machine-readable definition — type, choices, default, whether the
value is secret, which runtimes read it — is
[`contract/config.json`](../contract/config.json): the tables below, the Claude
Desktop bundle's settings form and the MCP Registry's `server.json` are generated
from it, and the unit suite fails if either runtime reads a variable it does not
declare.

<!-- BEGIN GENERATED: config-reference from contract/config.json by packages/server-node/scripts/config-artifacts.mjs. Do not edit by hand: edit the source, then run `npm run config:generate` in packages/server-node. -->

29 settings in six groups. A blank value means the default, whatever the type; a boolean accepts `1`, `true`, `yes`, `on` and `0`, `false`, `no`, `off`.

**Connection** — Which OPC UA server to talk to.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_SERVER_URL` | `opc.tcp://localhost:4840` | URL of the OPC UA server to connect to, including any path the server expects. Read once at startup: one process serves one endpoint. Nothing verifies who answers at this address unless the server certificate is pinned. |

**Channel security** — How the OPC UA secure channel is signed, encrypted and verified.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_SECURITY_POLICY` | `None` | Encryption suite to negotiate for the secure channel. Anything other than None needs a client certificate and key. The two AES suites are Node-only: the Python runtime refuses them at startup rather than downgrading. One of `None`, `Basic128Rsa15`, `Basic256`, `Basic256Sha256`, `Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss`. None is unencrypted and unauthenticated, and keeps control tools blocked unless insecure control is explicitly allowed. An unknown policy stops the server at startup. |
| `OPCUA_SECURITY_MODE` | SignAndEncrypt once a security policy is set, otherwise None | Message security mode for the secure channel. Sign authenticates messages without encrypting them. One of `None`, `Sign`, `SignAndEncrypt`. A policy with mode None, or a mode without a policy, stops the server at startup; leaving it blank never downgrades a policy to signing only. |
| `OPCUA_CLIENT_CERT` | — | Path to the certificate (PEM or DER) this client presents as its application identity. Required by any security policy other than None. A path that does not exist stops the server at startup. |
| `OPCUA_CLIENT_KEY` | — | Path to the private key matching the client certificate. A path, never key material. Whoever can read the file can impersonate this client: keep it readable only by the account running the server. A path that does not exist stops the server at startup. |
| `OPCUA_APPLICATION_URI` | the subjectAltName URI of the client certificate | Application URI announced to the server. Set it only for a certificate that carries no URI of its own: a server may reject a session whose URI does not match the certificate. |
| `OPCUA_SERVER_CERT` | — | Path to the OPC UA server's own certificate (PEM or DER), pinned: a server presenting any other certificate cannot complete the handshake. Control tools (writes, method calls, alarm actions) require it. Requires a security policy other than None. Unset, encryption protects against eavesdropping but not against an impostor endpoint, so control tools are refused unless OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL is set. Set without a security policy, it stops the server at startup rather than pinning nothing; a pinned certificate that has expired or is not yet valid refuses to connect. |

**User identity** — Who the OPC UA session logs in as.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_USERNAME` | — | OPC UA username. The session is anonymous when unset. Cannot be combined with a user certificate. Requires a password; either one alone stops the server at startup. |
| `OPCUA_PASSWORD` | — | **Secret.** Password for the username. Without a security policy it crosses the network in clear text. Supply it through the client's secret storage, never on a command line. |
| `OPCUA_USER_CERT` | — | Path to the certificate identifying the user, for X.509 user authentication: a different key pair from the client certificate, which secures the channel. Requires the user key and a security policy other than None; cannot be combined with a username. Any of those combinations broken stops the server at startup. |
| `OPCUA_USER_KEY` | — | Path to the private key for the user certificate. It signs the server's challenge and is never sent. Whoever can read the file can log in as this user: keep it readable only by the account running the server. |

**Tool policy** — Which MCP tools are offered, and what a control tool may touch.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_PROFILE` | `observe` | Which tools are offered. observe reads, browses, reads history and monitors; operator adds explicitly allowlisted writes, method calls and alarm actions; full exposes every tool and is meant only for a tightly scoped OPC UA account. One of `observe`, `operator`, `full` (`read-only` means `observe`; `readonly` means `observe`). Unset fails closed to observe; an unknown profile stops the server at startup. |
| `OPCUA_POLICY_FILE` | — | Path to a version-1 JSON policy file, the only place per-node value bounds can be set. Every policy variable that is set overrides what the file says. An unreadable or invalid file stops the server at startup. |
| `OPCUA_ALLOWED_TOOLS` | — | Comma-separated tool allowlist. It can only narrow the selected profile, never widen it. An unknown tool name stops the server at startup. |
| `OPCUA_ALLOWED_WRITE_NODES` | — | Comma-separated exact node IDs the operator profile may write, as ns=2;i=5 or, stable across a server restart, nsu=&lt;namespace-uri>;i=5. Setting it replaces the policy file's list, bounds included. Empty fails closed: operator can write nothing. A batch write with any target outside the list is refused before it reaches OPC UA. |
| `OPCUA_ALLOWED_METHODS` | — | Comma-separated object_node_id\|method_node_id pairs the operator profile may call. Empty fails closed: operator can call nothing. An entry without \| stops the server at startup. |
| `OPCUA_ALLOW_ACKNOWLEDGE_ALARMS` | `false` | Let the operator profile act on alarms: acknowledge_alarm and every act_on_alarm action. |
| `OPCUA_ALLOW_INSECURE_CONTROL` | `false` | Permit control tools on an OPC UA channel with no security policy (None). Does not cover a secured channel to an unverified server; that is OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL. Fail-open override: writes, method calls and alarm actions then travel unauthenticated and unencrypted. Reported as control=INSECURE-OVERRIDE in the startup line, get_server_status and every audit record. Never enable it against production equipment. |
| `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` | `false` | Permit control tools on a secured OPC UA channel whose server certificate is not pinned with OPCUA_SERVER_CERT. Does not cover a channel with no security policy; that is OPCUA_ALLOW_INSECURE_CONTROL. Fail-open override: writes, method calls and alarm actions are then encrypted to whichever server answered the endpoint, which may be an impostor. Reported as control=UNVERIFIED-OVERRIDE in the startup line, get_server_status and every audit record. Never enable it against production equipment. |
| `OPCUA_ALLOW_OUT_OF_RANGE_WRITES` | `false` | Allow a write outside the EURange the OPC UA server itself published for the node. Fail-open override: it discards the only value bound a deployment with no policy file has. |

**Audit** — Where the control audit trail goes, and what it is labelled with.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_AUDIT_FILE` | — | Path to an append-only file for the control audit trail, one JSON object per line, written beside stderr. Created owner-only (0600) if it does not exist; reopened when rotated away. A file that cannot be opened, or that is a symlink, not a regular file, another account's, or group/world-writable, stops the server rather than falling back to stderr alone. While set, a control call whose record cannot be written is refused (fail closed); reads continue. |
| `OPCUA_AUDIT_FSYNC` | `always` | When an audit-file record counts as written. always fsyncs each record before the control call proceeds, so it survives a power loss; none leaves it to the operating system, so it survives only a crash of this process. One of `always`, `none`. none can lose the records of control calls that already reached the plant if the machine loses power. |
| `OPCUA_AUDIT_CHAIN` | `none` | Adds seq, prev_hash and hash to every audit record so edits, deletions and reordering can be detected with --verify-audit. sha256 catches accidental or naive edits; hmac-sha256 also resists anyone without the key. Requires the audit file. One of `none`, `sha256`, `hmac-sha256`. Set without an audit file, or hmac-sha256 without a key file, stops the server. No chain detects a truncated tail. |
| `OPCUA_AUDIT_CHAIN_KEY_FILE` | — | Path to the HMAC key for the hmac-sha256 audit chain: at least 32 bytes, for example from openssl rand -hex 32. Surrounding whitespace is ignored. Whoever can read the key can forge the chain: a key readable or writable by group or others stops the server. Refused unless the chain is hmac-sha256. |
| `OPCUA_OPERATOR_ID` | — | Label stamped on every audit record as operator_label, so a shipped log says which deployment a control call came from. Configured, never verified: it is not an identity, and mcp_principal stays null. |

**Reconnection** — How a refused or dropped connection is retried.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_RECONNECT_INITIAL_DELAY_MS` | `1000` | Wait before the first attempt to repair a dropped or refused connection, in milliseconds. It doubles with each further attempt. |
| `OPCUA_RECONNECT_MAX_DELAY_MS` | `8000` | Ceiling for the doubling retry delay, in milliseconds. |
| `OPCUA_RECONNECT_MAX_RETRY` | `3` | Retries after the first attempt, per connection round, before a tool call gives up: a whole number from -1 to 1000. 0 never retries; -1 never stops trying, in bounded rounds of four retries so no single call waits forever. |
| `OPCUA_SESSION_TIMEOUT_MS` | `60000` | Session lifetime asked of the OPC UA server, in milliseconds. It also sets the keep-alive period, so it decides how quickly an idle connection notices the server has gone. |

<!-- END GENERATED: config-reference -->

## Deciding what the agent may do

Three profiles, and the default is the restrictive one:

| `OPCUA_PROFILE` | What it offers |
|---|---|
| `observe` *(default)* | Read, browse, history and monitoring. No writes, no methods |
| `operator` | The above, plus **only** the write targets and methods you allowlist |
| `full` | Every tool |

`operator` is the one worth understanding. A write to a node outside
`OPCUA_ALLOWED_WRITE_NODES` is refused before anything reaches OPC UA, and one
forbidden target rejects an entire batch rather than letting part of it through.
Both `operator` and `full` also require a **verified server**: a security
policy *and* the server's certificate pinned with `OPCUA_SERVER_CERT`. Encrypted
is not enough — without the pin the channel is encrypted to whoever answered.
Two lab-only overrides say otherwise in as many words, one per missing property:
`OPCUA_ALLOW_INSECURE_CONTROL=true` for a channel with no security, and
`OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true` for a secured one to an unpinned
server. A refused control call names the variable to set, and
`get_server_status` → `server_identity` says which of the three is in force
([SECURITY.md](../SECURITY.md#control-needs-a-verified-server)).

The policy is enforced again on **every call**, not only when tools are listed —
an MCP client may hold a stale catalogue, and a hidden tool is a usability
feature rather than a security boundary. Every control call is also recorded —
with its targets, its outcome, the endpoint it went to and the session it rode
on — to stderr and, if `OPCUA_AUDIT_FILE` is set, to an owner-only append-only
file beside it, fsync'd per record. A control call whose record cannot be written
is refused rather than sent; reads carry on. `OPCUA_AUDIT_CHAIN` adds an optional
hash chain that `opcua-mcp-server --verify-audit` checks. What that file can and
cannot prove — it is not a tamper-proof or authenticated record of *who* — is in
[SECURITY.md](../SECURITY.md#what-the-local-audit-file-can-and-cannot-prove).

## Bounding the value, not only the node

An allowlist says *where* a write may go. It does not say *what* may be written,
and for the one product category where a wrong number is a physical event that is
the weaker half: a model that has correctly identified the right setpoint and
hallucinated `9999` instead of `99.9` is fully allowlisted.

Two bounds now apply.

**The one the plant published.** An OPC UA `AnalogItemType` carries an `EURange` —
what the value holds in normal operation. Both servers read it, report it on every
reading, and refuse a write outside it before anything is sent. No configuration:
the equipment set the bound. Set `OPCUA_ALLOW_OUT_OF_RANGE_WRITES=true` for the
deployments that write outside normal operation on purpose.

**The one you write.** In a policy file, a `writable_nodes` entry may carry `min`,
`max`, `enum` or `max_change`:

```json
{
  "control": {
    "writable_nodes": [
      "nsu=urn:plant:line-a;s=Line1.SpeedSetpoint",
      { "node": "nsu=urn:plant:line-a;s=Line1.Temperature", "min": 0, "max": 120 },
      { "node": "nsu=urn:plant:line-a;s=Line1.Mode", "enum": ["AUTO", "MANUAL"] }
    ]
  }
}
```

A bare node id stays legal. Both bounds apply, so a policy can only ever narrow
what the equipment already allows. `OPCUA_ALLOWED_WRITE_NODES` is a comma-separated
list and cannot express a bound — a bounded node needs the file. The full shape is
in **[SECURITY.md](../SECURITY.md#bounding-the-value-not-only-the-node)**.

## Writing an allowlist that stays correct

`OPCUA_ALLOWED_WRITE_NODES` and `OPCUA_ALLOWED_METHODS` accept two forms:

```
ns=2;i=5                       # namespace index — resolved per session
nsu=urn:plant:line-a;i=5       # namespace URI — stable across sessions
```

**Prefer the second.** A namespace *index* is not a property of a node; it is
that node's position in the server's NamespaceArray for the current session. A
firmware update, an added namespace or a reordered load can move it — and an
allowlist written `ns=2;i=5` then authorises writes to a **different physical
node**, with nothing anywhere reporting that anything changed.

The namespace URI is the stable name. Both servers read the NamespaceArray on
every connect and resolve URI-pinned entries against it, so the allowlist follows
the node rather than the index. An entry naming a URI the server does not
publish matches nothing and is reported on stderr at connect time.

Spelling does not matter: `i=2253` and `ns=0;i=2253` are the same node, entries
are trimmed, and both runtimes canonicalise identically (pinned by
`tests/fixtures/node-id-forms.json`).

Larger deployments can put all of it in a version-1 JSON file
(`OPCUA_POLICY_FILE`) instead of the environment; the shape, and a worked
example, are in **[SECURITY.md](../SECURITY.md#tool-profiles-and-control-policy)**.

## Connecting securely

The defaults are unencrypted and unauthenticated, which suits the mock plant and
nothing else. A real deployment wants a policy, a client certificate, an identity
and a pinned server certificate — the last is what control tools require:

```bash
OPCUA_SECURITY_POLICY=Basic256Sha256     # implies SignAndEncrypt
OPCUA_CLIENT_CERT=/etc/opcua/client.pem  # this server's identity
OPCUA_CLIENT_KEY=/etc/opcua/client_key.pem
OPCUA_SERVER_CERT=/etc/opcua/server.pem  # pin the server you meant to reach
OPCUA_USERNAME=mcp-operator              # or OPCUA_USER_CERT for X.509
OPCUA_PASSWORD=…
```

Names are case-insensitive, and a policy on its own implies `SignAndEncrypt`.
Anything the OPC UA spec cannot honour — a mode without a policy, a policy
without a certificate, a username without a password, a path that does not exist
— is refused at startup with a message naming the variable, rather than failing
later against live equipment.

`OPCUA_USERNAME` / `OPCUA_PASSWORD` authenticate the session but encrypt nothing:
without a security policy the password crosses the network in clear text unless
the server's user-token policy protects it, and both servers say so on stderr.
Pair credentials with a policy.

Why each of these matters, what is still not protected, and the full X.509 story:
**[SECURITY.md](../SECURITY.md)**. Generating a certificate a server will accept —
including a file-naming trap on the Python runtime — is
**[docs/certificates.md](certificates.md)**.

## Staying connected

Neither server needs restarting when the OPC UA server does. A dropped
connection is retried with exponential backoff on the four
`OPCUA_RECONNECT_*` / `OPCUA_SESSION_TIMEOUT_MS` settings above, the read and
write paths transparently re-establish a dead session, and the data-change
subscriptions an agent is holding are re-created on the new session — the IDs
keep working and the values already buffered are still there to be read. The
two runtimes get there differently: node-opcua repairs a dropped channel in the
background and keeps the same session, while the Python runtime rebuilds a fresh
session when the next call needs one. The settings mean the same on both; see
[runtime differences](compatibility.md#runtime-differences).

Event subscriptions are re-created on a new session too, and the next `read_events` adds a
note that events raised while the connection was down were not received
(`read_event_history` can recover them from a server that stores them).

Stopped by `SIGTERM` or `SIGINT`, either server deletes its subscriptions and
closes the session before exiting 0, waiting at most five seconds for an OPC UA
server that has stopped answering.

Plant connectivity never gates the MCP protocol. Both servers answer
`initialize` at once and make their first connection in the background.
`tools/list` never touches the plant: it is the same list, answered at once,
whether the endpoint is up, down or coming back. A `get_server_status` that
arrives while that first round is still running waits for it for at most 3
seconds, so against a reachable plant the first status is already a connected
one, and against an unreachable one the server is diagnosable straight away. After that, reconnection is driven by
tool calls rather than by a timer: the next call that needs a session connects.
`get_server_status` is the one tool that answers either way — it reports
`connected: false` and the reason instead of failing (including "still
connecting" while a round is running, which it reports rather than waits for),
and every other tool's error points at it.

Retrying happens in *rounds*: one attempt plus `OPCUA_RECONNECT_MAX_RETRY`
retries, the delays doubling from `OPCUA_RECONNECT_INITIAL_DELAY_MS` up to
`OPCUA_RECONNECT_MAX_DELAY_MS`. A tool call that needs a session waits for at
most one round and then fails with "endpoint_offline: Not connected to the OPC
UA server at …".
The defaults (three retries, 1–8s apart) keep that short. Raise
`OPCUA_RECONNECT_MAX_RETRY` (up to 1000) for a site where outages are measured
in minutes; the last waiting a call will do is the sum of the delays. `-1` means
never give up — every later call starts another round — but each round is still
four retries long, so no single request, and not the server's start-up, can wait
forever.
