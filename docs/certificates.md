# Client certificates and trust setup

Any `OPCUA_SECURITY_POLICY` other than `None` needs a client certificate and its
private key: the identity the MCP server proves when it opens the secure
channel, and the thing an OPC UA server decides whether to trust. Neither
runtime will use a generated one for you — a key pair that appears by itself is
a key pair nobody owns — so this page covers making one that servers accept,
getting it trusted, and reading the failures when they are not. (node-opcua
does write a self-signed default into its own PKI folder the first time the Node
runtime connects, even unsecured; it is never used for a secured channel, which
refuses to start without `OPCUA_CLIENT_CERT`. See the
[runtime differences](compatibility.md#runtime-differences).)

Which variables to set: [Configuration](configuration.md). What the
servers verify and what they do not: [SECURITY.md](../SECURITY.md). A rehearsal
against a secured mock, with throwaway certificates:
[testing.md §4](testing.md#4-a-secured-connection-by-hand).

## What OPC UA asks of a certificate

More than the web does. A certificate that a browser would be happy with is
routinely refused by an OPC UA server, over fields TLS never looks at:

| Requirement | Why it matters |
|---|---|
| A `subjectAltName` **URI** entry | The server compares it with the ApplicationUri the session announces. Both runtimes announce the URI from this field, so it is the one identity you have to get right. Mismatch → `BadCertificateUriInvalid`. |
| `keyUsage` = `digitalSignature`, `nonRepudiation`, `keyEncipherment`, `dataEncipherment` | What the security policies actually use to sign and encrypt. The spec requires all four; node-opcua warns (`NODE-OPCUA-W16`) when one is missing, and servers may refuse outright. |
| `extendedKeyUsage` including `clientAuth` | Some servers check it; `serverAuth` alongside is harmless and what most OPC UA tooling emits. |
| `basicConstraints: CA:FALSE` | This is an application instance certificate, not a CA. |
| RSA 2048–4096, SHA-256 | `Basic256Sha256` needs SHA-256 and at least a 2048-bit key. |
| A validity window that covers now | Expired is refused, and node-opcua warns for ten days beforehand (`NODE-OPCUA-W05`). |

Self-signed is normal here, and is what the recipe below produces. OPC UA trust
is granted by listing a specific certificate on the server, not by chaining to a
public CA — an operator has to approve yours either way. Use a CA only if your
site already runs one and the server is configured to trust issuers.

## Generating one

With OpenSSL 1.1.1 or newer, in the directory where the key pair should live:

```bash
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -subj "/CN=OPC UA MCP Client/O=Example Plant" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment" \
  -addext "extendedKeyUsage=clientAuth,serverAuth" \
  -addext "subjectAltName=URI:urn:plant:mcp-client,DNS:$(hostname)" \
  -keyout client_key.pem -out client.pem
```

Pick your own `urn:` — it identifies this client to the server, is arbitrary but
must be unique per application instance, and is the value that ends up in the
session's ApplicationUri. `-days 825` is a common ceiling; shorter is fine if you
have somewhere to hang the renewal.

Check what you got before handing it to anything:

```bash
openssl x509 -in client.pem -noout -text | sed -n '/X509v3 extensions/,/Signature Algorithm/p'
```

Two file-naming rules worth following even where only one of them applies:

- **Name both files `*.pem`.** The Python runtime (python-opcua) decides PEM
  versus DER by extension alone, so a PEM key called `client.key` fails to load.
  The Node runtime sniffs the contents and accepts either name.
- **Leave the key unencrypted** (`-nodes` above) and protect it with file
  permissions — `chmod 600 client_key.pem`, owned by the account the MCP server
  runs as. Neither runtime can prompt for a passphrase: it is started by an MCP
  client, with no terminal to prompt on.

## Pointing the server at it

```bash
OPCUA_SECURITY_POLICY=Basic256Sha256    # implies SignAndEncrypt
OPCUA_CLIENT_CERT=/etc/opcua/client.pem
OPCUA_CLIENT_KEY=/etc/opcua/client_key.pem
```

`OPCUA_APPLICATION_URI` is not needed: both runtimes take the URI out of
`OPCUA_CLIENT_CERT`. Set it only for a certificate with no URI in its
`subjectAltName`, and expect a warning if it contradicts one that has it — the
certificate is what the server checks against.

Absolute paths. The MCP server is started by a desktop client, in whatever
working directory that client happens to have.

## Getting the certificate trusted

Real servers keep a trust list, and yours is not on it yet. The usual sequence,
whatever the vendor:

1. **Connect once and be refused** — `BadSecurityChecksFailed` or
   `BadCertificateUntrusted`. This is the step that hands the server your
   certificate.
2. **Find it in the server's rejected list.** Most implementations write it to a
   PKI directory laid out as `pki/rejected/certs` and `pki/trusted/certs`
   (node-opcua, open62541, the .NET stack), while Prosys, KEPServerEX, Siemens
   and the like surface the same thing in a certificate-manager UI.
3. **Move or approve it into the trusted list**, then connect again. Some servers
   need a restart or an explicit reload to notice.
4. **Authorize the user account too.** Trusting the certificate gets the channel
   open; `OPCUA_USERNAME` still has to be a user the server knows, with rights to
   the nodes you intend to read and write.

Do this by hand, once, before wiring the MCP client up to it — the failure is
much easier to read in a terminal than through an assistant. A fresh certificate
means a fresh thumbprint, so renewal repeats these steps.

## The other direction

Everything above gets *your* certificate trusted by the server. The reverse —
this client knowing it reached the right server — is `OPCUA_SERVER_CERT`, and
**control tools need it**: `operator` and `full` offer writes, method calls and
alarm actions only once the server's certificate is pinned.

Encrypted and authenticated are different properties. Without a pin, both
runtimes take the server's certificate from its endpoint description and encrypt
to it — which protects against eavesdropping, and not against whoever managed to
answer for the endpoint. A pin changes that: the handshake is encrypted to the
pinned key, so a server without the matching private key cannot complete it.

```bash
OPCUA_SERVER_CERT=/etc/opcua/plc_server.pem   # the server's certificate, PEM or DER
```

Getting the file, whatever the vendor:

- **From the server's own PKI** — usually `pki/own/certs` (node-opcua,
  open62541, the .NET stack), or an *export certificate* action in the
  certificate manager of Prosys, KEPServerEX, Siemens and the like. Best: it is
  the operator's copy, not the network's.
- **From the endpoint, once, on a network you trust.** node-opcua files every
  server certificate it has accepted in a per-user PKI folder
  (`~/Library/Preferences/node-opcua-default-nodejs/PKI` on macOS,
  `~/.config/node-opcua-default-nodejs/PKI` on Linux, under `%APPDATA%` on
  Windows); a first connection without a pin leaves it there. Compare its
  thumbprint with the one the server's UI shows before you trust it:
  `openssl x509 -in plc_server.pem -noout -fingerprint -sha256`.

What a pin does and does not check:

| Situation | What happens |
|---|---|
| The server presents the pinned certificate | Connects; `get_server_status` → `server_identity.server_authenticated: true`, `control: "secured"` |
| Anything else answers | The handshake fails — the impostor cannot decrypt it. No request of any kind reaches it |
| The pinned certificate has expired, or is not valid yet | Refused before connecting, naming the file and the date. Renew it on the server and pin the new one |
| No pin | Connects and reads; control tools are hidden, and a call to one is refused with `Set OPCUA_SERVER_CERT … or OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true` |

There is no CA trust store or revocation list: python-opcua has no server
certificate validation to build one on, and both runtimes keep one behaviour.
A pin is an exact match, so a revoked certificate is dealt with by replacing the
pin — and a server that renews its certificate has to be re-pinned, which is the
cost of the strongest check there is.

`OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true` offers control over an encrypted but
unverified channel, for a lab. It is not implied by
`OPCUA_ALLOW_INSECURE_CONTROL`, which covers only a channel with no security at
all, and whichever is in force is named in the startup line, in
`get_server_status` and in every audit record. See
[SECURITY.md](../SECURITY.md#connection-security-important).

## When it still will not connect

| Status code | What it means here |
|---|---|
| `BadCertificateUriInvalid` | The announced ApplicationUri is not the certificate's `subjectAltName` URI — usually an `OPCUA_APPLICATION_URI` left over from another certificate. Unset it. |
| `BadCertificateUntrusted`, `BadSecurityChecksFailed` | The certificate is not in the server's trust list yet — see above. |
| `BadCertificateTimeInvalid` | Expired, or not valid yet: check both ends of the validity window, and the clocks. |
| `BadCertificateUseNotAllowed` | `keyUsage` or `extendedKeyUsage` is missing what the server insists on. Regenerate with all four key usages. |
| `BadSecurityPolicyRejected`, `BadSecurityModeRejected` | The server offers no endpoint for the policy or mode you asked for. Its endpoint list will say which it does. |
| `BadUserAccessDenied`, `BadIdentityTokenRejected` | The channel is fine; `OPCUA_USERNAME` / `OPCUA_PASSWORD` are not. |
| `Configuration error: …` and an immediate exit | Not the server: the variables cannot be honoured as set, and the message names the one to fix. |

Both runtimes report on stderr, which is where an MCP client shows server logs.
A working connection says so:
`Connected to OPC UA server (policy=Basic256Sha256 mode=SignAndEncrypt user="mcp-operator")`.
