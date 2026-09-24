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
  /** Retries after the first attempt, per connection round. 0 disables
   *  retrying; -1 never gives up across rounds but still bounds each one — see
   *  `reconnectDelays`. A whole number from -1 to `MAX_RETRY_LIMIT`. */
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

/** The largest `OPCUA_RECONNECT_MAX_RETRY` accepted.
 *
 * Not a tuning knob: a refusal of a value that can only be a typo. A tool call
 * waits out one whole round, so a thousand retries at the default ceiling is
 * already over two hours inside one request — and `1e9` used to be accepted and
 * handed to a loop that summed a billion delays before the first request. The
 * Python server refuses above the same number with the same words.
 */
export const MAX_RETRY_LIMIT = 1000;

/** How many retries one connection round makes when `maxRetry` is -1.
 *
 * "Forever" cannot be the size of one round: the startup warm-up and every tool
 * call that needs a session wait for the round they start or join, and a round
 * that never ends is a request that never returns — which is what #136 was, on
 * this runtime, from `initialize` onwards. Retrying forever instead means no
 * round is ever the last: the next call starts another, and node-opcua's own
 * repair of an established channel has no limit at all. The Python server's
 * `_UNLIMITED_BUDGET_FACTOR` is the same number.
 */
export const UNLIMITED_ROUND_RETRIES = 4;

/** How long a request that reports on the connection — `tools/list` and
 * `get_server_status` — waits for the startup warm-up, in ms from its start.
 *
 * The MCP transport no longer waits for the warm-up at all (#136), so requests
 * can arrive while it is still connecting. Against a plant that is up it takes
 * well under this, and waiting for it is what keeps the first catalogue from
 * being the core tools only and the first status from reading "not connected".
 * Against one that is down it can take the whole round, and past this point
 * those two requests answer from what is known rather than wait on it. The
 * Python server's `WARM_UP_WAIT_MS` is the same number.
 */
export const WARM_UP_WAIT_MS = 3000;

function parseNumber(raw: string | undefined, name: string, fallback: number, min: number): number {
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < min) {
    throw new Error(`${name} must be a number >= ${min}, got "${raw}"`);
  }
  return value;
}

/** `OPCUA_RECONNECT_MAX_RETRY`: a whole number from -1 to `MAX_RETRY_LIMIT`.
 *
 * Stricter than the delays, because a count has no sensible fraction. `2.5`
 * used to be handed to node-opcua as it was while Python truncated it to 2, so
 * one setting meant two different things; `-0.5` passed the `>= -1` floor and
 * meant nothing at all. Digits only, so `0x10` and `1e2` — which `Number` reads
 * and Python's `int` does not — are refused on both rather than on one.
 */
function parseRetryCount(raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw.trim() === "") return fallback;
  const text = raw.trim();
  const value = /^[+-]?[0-9]+$/.test(text) ? Number(text) : NaN;
  if (!(value >= -1 && value <= MAX_RETRY_LIMIT)) {
    throw new Error(
      `OPCUA_RECONNECT_MAX_RETRY must be a whole number from -1 to ${MAX_RETRY_LIMIT}, got "${raw}"`
    );
  }
  // `-0` is a spelling of 0, not a sign.
  return value === 0 ? 0 : value;
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
    maxRetry: parseRetryCount(env.OPCUA_RECONNECT_MAX_RETRY, RECONNECT_DEFAULTS.maxRetry),
    sessionTimeout: parseNumber(
      env.OPCUA_SESSION_TIMEOUT_MS,
      "OPCUA_SESSION_TIMEOUT_MS",
      RECONNECT_DEFAULTS.sessionTimeout,
      1000
    ),
  };
}

/** The delays, in ms, between one connection attempt and the next in one round.
 *
 * The same sequence the Python server's `reconnect_delays` spells out, and for
 * the same settings: an unlimited `maxRetry` yields `UNLIMITED_ROUND_RETRIES`
 * delays rather than an endless list. Its length is the retry count handed to
 * node-opcua for a connect, which is what bounds a round on this runtime.
 */
export function reconnectDelays(config: ReconnectConfig): number[] {
  const retries = config.maxRetry < 0 ? UNLIMITED_ROUND_RETRIES : config.maxRetry;
  const delays: number[] = [];
  for (let attempt = 0; attempt < retries; attempt++) {
    delays.push(Math.min(config.initialDelay * 2 ** attempt, config.maxDelay));
  }
  return delays;
}

/** The `connectionStrategy` node-opcua is given for one connection round.
 *
 * Not the settings forwarded as they are, for two reasons. `maxRetry` is the
 * length of `reconnectDelays`, never -1 — see there. And the backoff library
 * underneath node-opcua throws unless `maxDelay` is strictly greater than
 * `initialDelay`, so settings Python accepts, such as 1000..1000, used to fail
 * every connect on this runtime at once with "The maximal backoff delay must be
 * greater than the initial backoff delay" — whether or not the plant was up.
 * When the ceiling is not above the start, every delay is the ceiling (that is
 * what `min` makes of it), so the round starts there, a millisecond under a
 * ceiling the library will accept.
 */
export function connectionStrategy(config: ReconnectConfig): {
  initialDelay: number;
  maxDelay: number;
  maxRetry: number;
} {
  const initialDelay = Math.max(1, Math.min(config.initialDelay, config.maxDelay));
  return {
    initialDelay,
    maxDelay: Math.max(config.maxDelay, initialDelay + 1),
    maxRetry: reconnectDelays(config).length,
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
  if (config.maxRetry < 0) return config.maxDelay * UNLIMITED_ROUND_RETRIES;
  const total = reconnectDelays(config).reduce((sum, delay) => sum + delay, 0);
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
