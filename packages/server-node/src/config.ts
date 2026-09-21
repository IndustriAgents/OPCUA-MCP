// Runtime configuration, read from the environment.

/** OPC UA endpoint the server and its capability probes connect to. */
export const SERVER_URL = process.env.OPCUA_SERVER_URL || "opc.tcp://localhost:4840";

/** How this server retries a connection that is refused or has dropped.
 *
 * The same four knobs, spelled the same way, exist in the Python server
 * (`config.py`): an operator who has tuned one deployment must not have to learn
 * a second vocabulary to tune the other.
 */
export interface ReconnectConfig {
  /** Delay before the first retry, in ms. Doubles each attempt up to `maxDelay`. */
  initialDelay: number;
  /** Ceiling for the doubling, in ms. */
  maxDelay: number;
  /** Retries after the first attempt. 0 disables retrying; -1 retries forever. */
  maxRetry: number;
  /** Session timeout asked of the OPC UA server, in ms. */
  sessionTimeout: number;
}

export const RECONNECT_DEFAULTS: ReconnectConfig = {
  initialDelay: 1000,
  maxDelay: 8000,
  maxRetry: 3,
  sessionTimeout: 60000,
};

function parseNumber(raw: string | undefined, name: string, fallback: number, min: number): number {
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < min) {
    throw new Error(`${name} must be a number >= ${min}, got "${raw}"`);
  }
  return value;
}

/** Parse the reconnection settings. Mirrored by the Python server's `parse_reconnect_config`. */
export function parseReconnectConfig(env: NodeJS.ProcessEnv): ReconnectConfig {
  return {
    initialDelay: parseNumber(
      env.OPCUA_RECONNECT_INITIAL_DELAY_MS,
      "OPCUA_RECONNECT_INITIAL_DELAY_MS",
      RECONNECT_DEFAULTS.initialDelay,
      0
    ),
    maxDelay: parseNumber(
      env.OPCUA_RECONNECT_MAX_DELAY_MS,
      "OPCUA_RECONNECT_MAX_DELAY_MS",
      RECONNECT_DEFAULTS.maxDelay,
      0
    ),
    // -1 is "forever", which is why the floor here is -1 rather than 0.
    maxRetry: parseNumber(
      env.OPCUA_RECONNECT_MAX_RETRY,
      "OPCUA_RECONNECT_MAX_RETRY",
      RECONNECT_DEFAULTS.maxRetry,
      -1
    ),
    sessionTimeout: parseNumber(
      env.OPCUA_SESSION_TIMEOUT_MS,
      "OPCUA_SESSION_TIMEOUT_MS",
      RECONNECT_DEFAULTS.sessionTimeout,
      1000
    ),
  };
}

/** How long to let an in-progress reconnection run before starting over, in ms.
 *
 * The sum of the delays the backoff will wait through, so the window a tool call
 * spends waiting for a repair to finish is exactly the window the operator
 * configured — no second knob that can contradict the first. An unlimited
 * `maxRetry` has no sum, so it is capped: waiting forever inside one tool call
 * is never the right answer, and giving up here only means rebuilding the client
 * from scratch, which is what the caller wanted anyway.
 */
export function reconnectBudgetMs(config: ReconnectConfig): number {
  if (config.maxRetry < 0) return config.maxDelay * 4;
  let total = 0;
  for (let attempt = 0; attempt < config.maxRetry; attempt++) {
    total += Math.min(config.initialDelay * 2 ** attempt, config.maxDelay);
  }
  return Math.max(total, config.initialDelay);
}

/** Reconnection settings read from the environment.
 *
 * Uncached, for the reason given on `toolPolicy()`: a memoised instance is what
 * made per-process state structural. These are immutable once parsed, so unlike
 * the policy nothing depends on two holders having the same object — but a
 * module-level cache here would still be a second thing to unpick before a
 * process can serve two endpoints.
 */
export function reconnectConfig(): ReconnectConfig {
  return parseReconnectConfig(process.env);
}

/** One-line, secret-free summary for the startup log. */
export function describeReconnect(config: ReconnectConfig): string {
  const retry = config.maxRetry < 0 ? "unlimited" : String(config.maxRetry);
  return `retries=${retry} backoff=${config.initialDelay}..${config.maxDelay}ms session-timeout=${config.sessionTimeout}ms`;
}
