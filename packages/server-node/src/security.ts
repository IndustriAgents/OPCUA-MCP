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
} from "node-opcua";

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
   * to be settable alongside the certificate. Unset leaves node-opcua's derived
   * default (`urn:<hostname>:<applicationName>`).
   */
  applicationUri?: string;
  /** Username identity; anonymous when unset. */
  username?: string;
  /** Password for `username`. May be empty, so `undefined` — not "" — means unset. */
  password?: string;
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

  const username = read(env, "OPCUA_USERNAME");
  // Not `read`: an empty password is a real (if unwise) credential, so only an
  // absent variable counts as unset here.
  const password = env.OPCUA_PASSWORD;
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
  const user = config.username === undefined ? "anonymous" : `"${config.username}"`;
  return `policy=${config.policy} mode=${config.mode} user=${user}`;
}

/** True when the connection carries neither encryption/signing nor a user. */
export function isInsecure(config: SecurityConfig): boolean {
  return config.policy === "None" && config.username === undefined;
}

/** Client options that select the configured policy, mode, certificate and URI. */
export function clientSecurityOptions(config: SecurityConfig) {
  return {
    securityMode: MessageSecurityMode[config.mode],
    securityPolicy: SecurityPolicy[config.policy],
    ...(config.clientCert ? { certificateFile: config.clientCert } : {}),
    ...(config.clientKey ? { privateKeyFile: config.clientKey } : {}),
    ...(config.applicationUri ? { applicationUri: config.applicationUri } : {}),
  };
}

/** The identity to activate the session with. */
export function userIdentity(config: SecurityConfig): UserIdentityInfo {
  if (config.username === undefined) {
    return { type: UserTokenType.Anonymous };
  }
  return {
    type: UserTokenType.UserName,
    userName: config.username,
    password: config.password ?? "",
  };
}
