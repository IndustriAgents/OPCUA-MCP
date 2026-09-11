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
OPCUA_USERNAME=mcp-operator
OPCUA_PASSWORD=…
# and, when the server checks it against the certificate:
OPCUA_APPLICATION_URI=urn:plant:mcp-client
```

An unusable combination — a mode without a policy, a policy without a client
certificate, a username without a password, a certificate path that does not
exist — is rejected at startup rather than at the first tool call.

What this does **not** do, and you should still plan for:

- **Server certificate verification.** The server's certificate is taken from
  its endpoint description during the handshake; neither runtime pins it or
  validates it against a trust list, so encryption here protects against passive
  eavesdropping, not against an attacker who can impersonate the endpoint.
- **Protecting credentials on an unsecured channel.** `OPCUA_USERNAME` /
  `OPCUA_PASSWORD` without a security policy is authentication, not
  confidentiality: both client libraries send the password in clear text when
  the server's user-token policy specifies no security policy of its own. Both
  servers warn about this on stderr; set `OPCUA_SECURITY_POLICY` rather than
  relying on the server to encrypt the token.
- **Certificate-based *user* authentication** (`X509IdentityToken`). User
  identity is anonymous or username/password only.
- **Input validation on node IDs and written values**, beyond what the OPC UA
  server itself enforces.
- **Secret handling.** `OPCUA_PASSWORD` is read from the environment, so it is
  as protected as the MCP client config file that holds it.

Treat the MCP servers as having the same privileges as the OPC UA account they
connect with: anyone able to talk to the MCP server can read and write any node
that account can.

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
