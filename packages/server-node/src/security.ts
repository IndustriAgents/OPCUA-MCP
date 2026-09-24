// OPC UA connection security, configured from the environment.
//
// The defaults are `None`/`None` — unencrypted and unauthenticated — which is
// what the bundled mock expects and what every release up to 0.2.1 hardcoded.
// Anything pointed at real equipment should set at least a policy and a mode.
//
// The Python runtime mirrors this module (packages/server-python/src/
// opcua_mcp_server/security.py): same variables, same defaults, same error
// wording, so a config that works against one runtime works against the other.
import {
  MessageSecurityMode,
  SecurityPolicy,
  type UserIdentityInfo,
  UserTokenType,
} from "node-opcua-client";
import {
  exploreCertificate,
  keyOperationsFromPrivateKey,
  readCertificate,
  readPrivateKey,
} from "node-opcua-crypto";

import { existsSync } from "fs";

/** Policies both runtimes accept. */
export const SHARED_POLICIES = ["None", "Basic128Rsa15", "Basic256", "Basic256Sha256"] as const;

/** Policies only `node-opcua` implements; the Python runtime rejects these. */
export const NODE_ONLY_POLICIES = ["Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss"] as const;

export const POLICIES = [...SHARED_POLICIES, ...NODE_ONLY_POLICIES] as const;

export const MODES = ["None", "Sign", "SignAndEncrypt"] as const;

export type PolicyName = (typeof POLICIES)[number];
export type ModeName = (typeof MODES)[number];

/** Resolved connection security: what to negotiate and who to log in as. */
export interface SecurityConfig {
  policy: PolicyName;
  mode: ModeName;
  /** Client certificate, required for any policy other than `None`. */
  clientCert?: string;
  /** Private key for `clientCert`. */
  clientKey?: string;
  /**
   * Application URI announced to the server.
   *
   * OPC UA servers may reject a session whose ApplicationDescription URI does
   * not match the `subjectAltName` URI of the client certificate, so this has
   * to be settable alongside the certificate. Unset, node-opcua takes the URI
   * out of `clientCert` itself, falling back to `urn:<hostname>:<applicationName>`
   * when there is no certificate or it carries no URI. The Python runtime now
   * does the same (`certificate_application_uri` in its `security.py`), so the
   * same files announce the same identity on either runtime.
   */
  applicationUri?: string;
  /** Username identity; anonymous when unset. */
  username?: string;
  /** Password for `username`. May be empty, so `undefined` — not "" — means unset. */
  password?: string;
  /**
   * The OPC UA *server* certificate this client expects, pinned.
   *
   * Unset, both client libraries take whatever certificate the endpoint
   * presents and encrypt to it — which protects against passive eavesdropping
   * but not against whoever managed to answer. Set, a server presenting
   * anything else cannot complete the handshake.
   */
  serverCert?: string;
  /**
   * Certificate identifying the *user*, for X.509 authentication.
   *
   * Deliberately not `clientCert`. That one secures the channel and is the
   * application's identity; this one is the user's, is a different key pair,
   * and is what the server checks against its user list. Conflating the two is
   * the obvious way to get this wrong, so they are named apart and validated
   * apart.
   */
  userCert?: string;
  /** Private key for `userCert`. Signs the server's challenge; never sent. */
  userKey?: string;
}

/** Reports whether a path exists; injectable so the parser stays testable. */
export type PathExists = (path: string) => boolean;

/**
 * An environment variable's value, or undefined when unset or blank.
 *
 * Blank counts as unset because MCP client configs routinely carry empty `env`
 * entries, and `OPCUA_SECURITY_POLICY=""` clearly means "not configured".
 */
function read(env: NodeJS.ProcessEnv, name: string): string | undefined {
  const value = env[name]?.trim();
  return value ? value : undefined;
}

/** The entry of `names` matching `value` case-insensitively, if any. */
function canonicalize<T extends string>(names: readonly T[], value: string): T | undefined {
  return names.find((name) => name.toLowerCase() === value.toLowerCase());
}

/**
 * Read and validate the security configuration from an environment.
 *
 * Throws on any combination OPC UA cannot honour, rather than letting it fail
 * later as an opaque library error against real equipment.
 */
export function parseSecurityConfig(
  env: NodeJS.ProcessEnv,
  exists: PathExists = existsSync
): SecurityConfig {
  const rawPolicy = read(env, "OPCUA_SECURITY_POLICY");
  const rawMode = read(env, "OPCUA_SECURITY_MODE");

  const policy = rawPolicy === undefined ? "None" : canonicalize(POLICIES, rawPolicy);
  if (!policy) {
    throw new Error(
      `Invalid OPCUA_SECURITY_POLICY: "${rawPolicy}". Use one of: ${POLICIES.join(", ")}`
    );
  }

  const mode = rawMode === undefined ? undefined : canonicalize(MODES, rawMode);
  if (rawMode !== undefined && !mode) {
    throw new Error(`Invalid OPCUA_SECURITY_MODE: "${rawMode}". Use one of: ${MODES.join(", ")}`);
  }

  if (policy === "None" && mode !== undefined && mode !== "None") {
    throw new Error(
      `OPCUA_SECURITY_MODE=${mode} requires OPCUA_SECURITY_POLICY to be set to a policy ` +
        `other than None`
    );
  }
  if (policy !== "None" && mode === "None") {
    throw new Error(
      `OPCUA_SECURITY_POLICY=${policy} cannot be combined with OPCUA_SECURITY_MODE=None; ` +
        `use Sign or SignAndEncrypt`
    );
  }

  // A policy on its own implies the strongest mode it can carry: asking for
  // encryption and getting only signing would be a silent downgrade.
  const resolvedMode: ModeName = mode ?? (policy === "None" ? "None" : "SignAndEncrypt");

  const clientCert = read(env, "OPCUA_CLIENT_CERT");
  const clientKey = read(env, "OPCUA_CLIENT_KEY");
  if (policy !== "None" && !(clientCert && clientKey)) {
    throw new Error(
      `OPCUA_SECURITY_POLICY=${policy} requires OPCUA_CLIENT_CERT and OPCUA_CLIENT_KEY ` +
        `(paths to the client certificate and its private key)`
    );
  }
  for (const [name, path] of [
    ["OPCUA_CLIENT_CERT", clientCert],
    ["OPCUA_CLIENT_KEY", clientKey],
  ] as const) {
    if (path && !exists(path)) {
      throw new Error(`${name} does not exist: ${path}`);
    }
  }

  const applicationUri = read(env, "OPCUA_APPLICATION_URI");

  // --- server certificate verification ---------------------------------------
  const serverCert = read(env, "OPCUA_SERVER_CERT");
  if (serverCert !== undefined && policy === "None") {
    // Refused rather than ignored. With no channel security the server's
    // certificate is never exchanged, so pinning it would verify nothing while
    // reading, in a config file, exactly like protection. A security control
    // that silently does nothing is worse than its absence.
    throw new Error(
      "OPCUA_SERVER_CERT requires OPCUA_SECURITY_POLICY to be set to a policy other than " +
        "None; with no channel security the server presents no certificate to verify"
    );
  }

  // --- X.509 user authentication ----------------------------------------------
  const userCert = read(env, "OPCUA_USER_CERT");
  const userKey = read(env, "OPCUA_USER_KEY");
  if ((userCert === undefined) !== (userKey === undefined)) {
    throw new Error(
      "OPCUA_USER_CERT and OPCUA_USER_KEY must be set together (the user's certificate and " +
        "the private key that signs the server's challenge)"
    );
  }
  if (userCert !== undefined && policy === "None") {
    throw new Error(
      "OPCUA_USER_CERT requires OPCUA_SECURITY_POLICY to be set to a policy other than None; " +
        "the certificate challenge is signed over the server certificate, which an unsecured " +
        "channel does not carry"
    );
  }

  for (const [name, path] of [
    ["OPCUA_SERVER_CERT", serverCert],
    ["OPCUA_USER_CERT", userCert],
    ["OPCUA_USER_KEY", userKey],
  ] as const) {
    if (path && !exists(path)) {
      throw new Error(`${name} does not exist: ${path}`);
    }
  }

  const username = read(env, "OPCUA_USERNAME");
  if (userCert !== undefined && username !== undefined) {
    // One session carries one user identity token. Accepting both would mean
    // choosing one silently, and the one not chosen is the one the operator
    // thinks is in force.
    throw new Error(
      "OPCUA_USER_CERT cannot be combined with OPCUA_USERNAME; a session has one user " +
        "identity, so use either certificate or username authentication"
    );
  }
  // Not `read`: an empty password is a real (if unwise) credential, so only an
  // absent variable counts as unset — except when there is no username to pair
  // it with, where a blank value can only mean "not configured". MCP client
  // configs routinely carry empty env entries, and an MCP bundle substitutes an
  // unset optional field as an empty string, so treating that pair as a usage
  // error would make an anonymous connection impossible to express there.
  const password = username === undefined && !env.OPCUA_PASSWORD ? undefined : env.OPCUA_PASSWORD;
  if (username !== undefined && password === undefined) {
    throw new Error("OPCUA_USERNAME requires OPCUA_PASSWORD");
  }
  if (username === undefined && password !== undefined) {
    throw new Error("OPCUA_PASSWORD requires OPCUA_USERNAME");
  }

  return {
    policy,
    mode: resolvedMode,
    clientCert,
    clientKey,
    applicationUri,
    username,
    password,
    serverCert,
    userCert,
    userKey,
  };
}

let cached: SecurityConfig | null = null;

/** The process-wide security configuration, parsed once. */
export function securityConfig(): SecurityConfig {
  if (!cached) {
    cached = parseSecurityConfig(process.env);
  }
  return cached;
}

/** One-line, secret-free summary for the startup log. */
export function describeSecurity(config: SecurityConfig): string {
  const user =
    config.userCert !== undefined
      ? "certificate"
      : config.username === undefined
        ? "anonymous"
        : `"${config.username}"`;
  // `server-cert=` only when pinning is on: a line that said `pinned` vs
  // `unpinned` on every startup would train the reader to skip it, and this is
  // the one word that distinguishes "encrypted" from "encrypted to whoever
  // answered".
  const pinned = config.serverCert === undefined ? "" : " server-cert=pinned";
  return `policy=${config.policy} mode=${config.mode} user=${user}${pinned}`;
}

/**
 * Warnings to log before connecting; empty once a policy is configured.
 *
 * Keyed on the policy alone, never on the presence of a user: a username
 * authenticates the session but leaves every read, write and method call on the
 * wire in the clear, so credentials must not buy silence here. The password may
 * be among what is in the clear — both client libraries send it unencrypted when
 * the server's user-token policy specifies no security policy of its own.
 */
export function securityWarnings(config: SecurityConfig): string[] {
  if (config.policy !== "None") {
    // Encryption without a pinned server certificate is encryption to whoever
    // answered — DNS, ARP, a compromised switch or a mistyped endpoint all
    // reach it. Said once, on a secured connection, because this is the gap a
    // reader of `policy=Basic256Sha256` is least likely to suspect.
    return config.serverCert === undefined
      ? [
          "the OPC UA server's certificate is not being verified — set OPCUA_SERVER_CERT to " +
            "pin it. Encryption without it protects against passive eavesdropping, not " +
            "against an attacker who can impersonate the endpoint, so control tools stay " +
            "disabled unless OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true.",
        ]
      : [];
  }

  const warnings = [
    "connecting with no OPC UA security (policy=None) — traffic is unencrypted and " +
      "unsigned. Set OPCUA_SECURITY_POLICY for anything beyond local development.",
  ];
  if (config.username !== undefined) {
    warnings.push(
      "OPCUA_USERNAME/OPCUA_PASSWORD are being sent over that unencrypted channel, and " +
        "the password is in clear text unless the server's user-token policy encrypts it."
    );
  }
  return warnings;
}

/** A validity bound as the refusal words it: ISO-8601 UTC, to the second.
 *  `security.py` formats the same instant the same way. */
function isoSecond(moment: Date): string {
  return moment.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Why the pinned server certificate cannot vouch for the server, or null.
 *
 * Checked at every connect rather than once at startup, because a certificate
 * expires while a process runs. Neither client library looks at the validity
 * window of a pinned certificate — python-opcua only encrypts to its key — so
 * without this an expired pin would keep "verifying" a server whose identity
 * document its owner has retired.
 *
 * Fails closed: the connection is refused with this as the reason, rather than
 * carrying on unverified, because the operator said which server this is and
 * the evidence for it is no longer valid. An unreadable file is left to the
 * library, which refuses it in its own words.
 *
 * `pinned_certificate_problem` in `security.py` is the other half.
 */
export function pinnedCertificateProblem(path: string, now: Date = new Date()): string | null {
  let notBefore: Date;
  let notAfter: Date;
  try {
    ({ notBefore, notAfter } = exploreCertificate(readCertificate(path)).tbsCertificate.validity);
  } catch {
    return null;
  }
  if (now > notAfter) {
    return (
      `OPCUA_SERVER_CERT ${path} expired on ${isoSecond(notAfter)}, so it cannot verify the ` +
      `server. Pin the certificate the server presents now, renewing it on the server first ` +
      `if that is the one that expired.`
    );
  }
  if (now < notBefore) {
    return (
      `OPCUA_SERVER_CERT ${path} is not valid until ${isoSecond(notBefore)}, so it cannot ` +
      `verify the server yet. Check this machine's clock, or pin the certificate the server ` +
      `presents now.`
    );
  }
  return null;
}

/** Client options that select the configured policy, mode, certificate and URI.
 *
 * `serverCertificate`, when pinned, is also what spares python-opcua's client
 * the extra endpoint round-trip it otherwise makes to fetch one — the two
 * runtimes end up doing the same thing for the same reason.
 */
export function clientSecurityOptions(config: SecurityConfig) {
  if (config.serverCert) {
    const problem = pinnedCertificateProblem(config.serverCert);
    if (problem !== null) throw new Error(problem);
  }
  return {
    securityMode: MessageSecurityMode[config.mode],
    securityPolicy: SecurityPolicy[config.policy],
    ...(config.clientCert ? { certificateFile: config.clientCert } : {}),
    ...(config.clientKey ? { privateKeyFile: config.clientKey } : {}),
    ...(config.applicationUri ? { applicationUri: config.applicationUri } : {}),
    ...(config.serverCert ? { serverCertificate: readCertificate(config.serverCert) } : {}),
  };
}

/** The identity to activate the session with.
 *
 * Three mutually exclusive forms, and `parseSecurityConfig` has already refused
 * any combination that would express two at once — so the order of these checks
 * cannot silently pick a winner.
 */
export function userIdentity(config: SecurityConfig): UserIdentityInfo {
  if (config.userCert !== undefined && config.userKey !== undefined) {
    return {
      type: UserTokenType.Certificate,
      certificateData: readCertificate(config.userCert),
      // `keyOperations`, not the deprecated raw-PEM `privateKey`: the key signs
      // the server's challenge through a provider and never becomes a string in
      // this process. python-opcua holds the key in memory either way, so this
      // is one place the runtimes genuinely differ — in Node's favour.
      keyOperations: keyOperationsFromPrivateKey(readPrivateKey(config.userKey)),
    };
  }
  if (config.username === undefined) {
    return { type: UserTokenType.Anonymous };
  }
  return {
    type: UserTokenType.UserName,
    userName: config.username,
    password: config.password ?? "",
  };
}
