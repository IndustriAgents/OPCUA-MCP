# Security Policy

## Connection security (important)

By default — with no security variables set — both server implementations
connect using `SecurityPolicy.None` and `MessageSecurityMode.None`, so traffic
is **unencrypted and unauthenticated**. That default suits local development and
evaluation against mock/test servers, **not** production or anything exposed to
an untrusted network. Both servers log a warning to stderr while running that
way.

For anything else, configure security through the environment (identical
variables on both runtimes, documented in the
[README](README.md#configuration)):

```bash
OPCUA_SECURITY_POLICY=Basic256Sha256   # implies SignAndEncrypt
OPCUA_CLIENT_CERT=/etc/opcua/client.pem
OPCUA_CLIENT_KEY=/etc/opcua/client_key.pem
OPCUA_SERVER_CERT=/etc/opcua/server.pem  # pin the server; see below
OPCUA_USERNAME=mcp-operator
OPCUA_PASSWORD=…
```

**Set `OPCUA_SERVER_CERT`.** Without it the server's certificate is whatever the
endpoint presented, so the channel is encrypted *to whoever answered* — DNS, ARP,
a compromised switch or a mistyped endpoint all reach that. Both servers say so
on stderr when it is unset, on an otherwise fully secured connection, because
`policy=Basic256Sha256 mode=SignAndEncrypt` reads like the connection is safe and
the one thing it does not establish is who is on the other end.

For X.509 *user* authentication, set `OPCUA_USER_CERT` and `OPCUA_USER_KEY`
instead of `OPCUA_USERNAME`/`OPCUA_PASSWORD`. That is a **different key pair**
from `OPCUA_CLIENT_CERT`: the client certificate is the application's identity
and secures the channel, the user certificate is the user's and is what the
server checks against its user list. Configuring both a user certificate and a
username is refused at startup — a session carries one identity, and silently
picking one would leave the operator believing the other was in force.

The ApplicationUri the session announces is taken from the `subjectAltName` of
the client certificate, which is what servers check it against;
`OPCUA_APPLICATION_URI` overrides that, for a certificate that carries no URI.
Generating a certificate servers accept, and getting it into a server's trust
list, is [docs/certificates.md](docs/certificates.md).

An unusable combination — a mode without a policy, a policy without a client
certificate, a username without a password, a certificate path that does not
exist — is rejected at startup rather than at the first tool call.

What this does **not** do, and you should still plan for:

- **Trust-list validation of the server certificate.** `OPCUA_SERVER_CERT` pins
  one expected certificate, which is the strongest option here and the one to
  use; what is *not* implemented is a CA trust store with revocation checking,
  so a deployment rotating server certificates has to update the pinned file.
  Leaving `OPCUA_SERVER_CERT` unset falls back to the old behaviour — the
  certificate is taken from the endpoint description and not verified at all.
  The other direction is checked by the server: it decides whether to trust the
  client certificate you configure.
- **Protecting credentials on an unsecured channel.** `OPCUA_USERNAME` /
  `OPCUA_PASSWORD` without a security policy is authentication, not
  confidentiality: both client libraries send the password in clear text when
  the server's user-token policy specifies no security policy of its own. Both
  servers warn about this on stderr; set `OPCUA_SECURITY_POLICY` rather than
  relying on the server to encrypt the token.
- **Input validation on node IDs and written values**, beyond what the OPC UA
  server itself enforces.
- **Secret handling.** `OPCUA_PASSWORD` is read from the environment, so it is
  as protected as the MCP client config file that holds it.

Treat the MCP servers as having the same privileges as the OPC UA account they
connect with: anyone able to talk to the MCP server can read and write any node
that account can.

## Tool profiles and control policy

Connection security decides who can talk to the OPC UA server. This decides what
the *agent* may do once connected, and the two are independent: an encrypted,
authenticated session to an account with write permission is still a session an
agent can write through.

The default profile is `observe` — read, browse, history and monitoring, with no
writes and no method calls. That is the right default because an MCP server is
driven by a model, and "it should not have been able to do that" is a much worse
outcome than "it could not do that".

`operator` adds control, but only to targets named in advance:

```json
{
  "version": 1,
  "profile": "operator",
  "allowed_tools": ["read_opcua_nodes", "write_opcua_nodes", "call_opcua_method"],
  "control": {
    "writable_nodes": ["nsu=urn:plant:line-a;s=Line1.SpeedSetpoint"],
    "callable_methods": [
      { "object_id": "nsu=urn:plant:line-a;s=Line1", "method_id": "nsu=urn:plant:line-a;s=Line1.Reset" }
    ],
    "acknowledge_alarms": false
  }
}
```

Point `OPCUA_POLICY_FILE` at it. Environment variables override the file, so a
deployment can ship one policy and narrow it per host.

**Write the allowlist with `nsu=<namespace-uri>;…`, not `ns=<index>;…`.** A
namespace *index* is that node's position in the server's NamespaceArray for the
current session — a firmware update or a reordered namespace load can move it,
and an allowlist written `ns=2;i=5` then authorises writes to a **different
physical node** with nothing reporting that anything changed. Both servers read
the NamespaceArray on every connect and resolve URI-pinned entries against it. An
entry naming a URI the server does not publish matches nothing and is reported on
stderr.

Three properties worth knowing:

- **Enforced on every call**, not only when tools are listed. An MCP client may
  hold a stale catalogue, so hiding a tool is a usability feature and the
  authorisation check is the boundary.
- **A batch is all-or-nothing.** One forbidden target rejects the whole write
  before any of it is sent, so a batch can never end up partially applied.
- **Control needs a secured channel.** `operator` and `full` refuse to offer
  control tools over an unencrypted connection unless
  `OPCUA_ALLOW_INSECURE_CONTROL=true` is set explicitly, and the startup line
  then reads `control=INSECURE-OVERRIDE` rather than blending in.

### What is audited

Every `control` and `alarm-action` call writes one JSON line to **stderr**:

```json
{"event":"opcua_mcp_policy","timestamp":"2026-09-18T09:12:44.001Z","profile":"operator",
 "tool":"write_opcua_nodes","decision":"allowed","node_ids":["ns=2;i=13"]}
```

`decision` is `allowed`, `denied`, `completed` or `failed` — the outcome as well
as the verdict, because a call that was permitted and a call that reached the
plant are different facts. Reads are never audited. Neither credentials nor the
values being written appear in a record, and a test asserts it.

It is **not durable**: nothing here writes a file or survives the process. For a
retained record, collect the server's stderr — the format is stable and
line-oriented for exactly that.

## Supported versions

This project is pre-1.0. Security fixes land on `main` and the latest published
npm release of `opcua-mcp-server`.

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, report privately via GitHub's
[private vulnerability reporting](https://github.com/midhunxavier/OPCUA-MCP/security/advisories/new),
or email the maintainer at midhunxavier@outlook.com.

Please include:

- A description of the issue and its impact
- Steps to reproduce (or a proof of concept)
- Affected version(s)/commit

We aim to acknowledge reports within a few days and will coordinate a fix and
disclosure timeline with you.
