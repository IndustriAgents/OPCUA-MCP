/** What the OPC UA server can do, and what a call that needs it is told (#140).
 *
 * A capability used to decide what `tools/list` *said*: `read_event_history` was
 * missing from the catalogue of a server without an event archive, and
 * `read_opcua_history` lost its `aggregate_function` argument on one without
 * aggregates. That made plant availability part of the MCP interface. A server
 * started while the plant was down listed neither, and when the plant came back
 * nothing portable told the client to list again — clients and models commonly
 * hold a tool list for the life of a session, and `notifications/tools/list_changed`
 * cannot be sent the same way by both SDKs (docs/architecture.md). So the
 * catalogue is now the contract, always, and the capability is checked where it
 * can actually be known: on the call, against the live session.
 *
 * The answers are cached per OPC UA session. A new session — a rebuild, or a
 * session the client library had to re-create after a server restart — is a new
 * *generation*, and answers read on an older one are never trusted: a restarted
 * server may not be the same server. `get_server_status` reports the cache as it
 * stands, with the generation and time it was read.
 *
 * `capabilities.py` is the Python half, and `tests/fixtures/capability-gate.json`
 * pins the two to the same decisions and the same words.
 */

import { CONTRACT, type CapabilitySpec, type ToolSpec } from "./contract.js";
import { message } from "./errors.js";

/** One answer: the server said yes, the server said no, or nobody could ask it. */
export type Support = "supported" | "not_supported" | "unknown";

/** What one probe found, and why it found nothing when it could not ask. */
export interface Probe {
  support: Support;
  /** Why the answer is `unknown`; null otherwise. */
  reason: string | null;
  /** True when an `unknown` is because the session died under the probe —
   *  worth rebuilding and asking again, where a timeout is not. */
  connectionLost?: boolean;
}

/** The capabilities the contract defines, in its order. `$` keys are prose. */
export const CAPABILITY_NAMES: readonly string[] = Object.keys(CONTRACT.capabilities).filter(
  (name) => !name.startsWith("$")
);

/** Why an answer is `unknown` before any session has been asked. */
export const NOT_YET_ASKED = "not yet asked: no OPC UA session has been established";

/** Everything known about the server's optional features, for one session. */
export interface CapabilityAnswers {
  /** The session generation these were read on; null before any was asked. */
  generation: number | null;
  /** When, ISO-8601 UTC; null before any session was asked. */
  checkedAt: string | null;
  support: Record<string, Support>;
  /** Why each `unknown` is unknown. */
  reasons: Record<string, string>;
  /** The aggregate functions the server offers, by OPC UA name. */
  aggregateFunctions: string[];
  /** True when a probe behind these lost the session it was asking on. */
  connectionLost: boolean;
}

/** The answers before any session has been asked: everything unknown. */
export function unasked(): CapabilityAnswers {
  return {
    generation: null,
    checkedAt: null,
    support: Object.fromEntries(CAPABILITY_NAMES.map((name) => [name, "unknown" as Support])),
    reasons: Object.fromEntries(CAPABILITY_NAMES.map((name) => [name, NOT_YET_ASKED])),
    aggregateFunctions: [],
    connectionLost: false,
  };
}

/** Assemble one session's answers from its probes. */
export function answersFrom(
  generation: number | null,
  checkedAt: string,
  probes: Record<string, Probe>,
  aggregateFunctions: string[]
): CapabilityAnswers {
  const answers: CapabilityAnswers = {
    generation,
    checkedAt,
    support: {},
    reasons: {},
    aggregateFunctions,
    connectionLost: false,
  };
  for (const name of CAPABILITY_NAMES) {
    const probe = probes[name] ?? { support: "unknown", reason: NOT_YET_ASKED };
    answers.support[name] = probe.support;
    if (probe.connectionLost) answers.connectionLost = true;
    if (probe.support === "unknown") answers.reasons[name] = probe.reason ?? "no answer";
  }
  return answers;
}

/** What a call needs, as groups of which each must have one capability supported.
 *
 * The tool's own `capabilities` are one group; each gated argument the call
 * actually passes adds another. `read_opcua_history` needs history *or*
 * aggregates, and with `aggregate_function` it needs aggregates as well — which
 * is what hiding the argument used to express.
 */
export function requirements(tool: ToolSpec, args: Record<string, unknown>): string[][] {
  const groups = tool.capabilities.length > 0 ? [tool.capabilities] : [];
  for (const [argument, needs] of Object.entries(tool.argumentCapabilities ?? {})) {
    if (args[argument] !== undefined && args[argument] !== null) groups.push(needs);
  }
  return groups;
}

/** Whether a call may go ahead, and if not, which group stopped it and why. */
export type Verdict =
  | { outcome: "allowed" }
  | { outcome: "capability_not_supported" | "capability_unknown"; capabilities: string[] };

/** Decide a call from its requirement groups and the answers.
 *
 * A group is met by any one supported member. One that is not met is `unknown`
 * if any member is unknown — the server may yet say yes, and refusing it as
 * unsupported would be stating something nobody found out — and otherwise
 * `not_supported`.
 */
export function verdict(groups: string[][], support: Record<string, Support>): Verdict {
  for (const group of groups) {
    if (group.some((name) => support[name] === "supported")) continue;
    const outcome = group.some((name) => support[name] !== "not_supported")
      ? "capability_unknown"
      : "capability_not_supported";
    return { outcome, capabilities: group };
  }
  return { outcome: "allowed" };
}

function spec(name: string): CapabilitySpec {
  return CONTRACT.capabilities[name];
}

/** "historical data access (AccessHistoryDataCapability, ns=0;i=11193) or …" */
function requirement(capabilities: string[]): string {
  return capabilities
    .map((name) => `${spec(name).label} (${spec(name).browseName}, ${spec(name).nodeId})`)
    .join(" or ");
}

/** The refusal for a call `verdict` did not allow, from the contract's templates.
 *
 * The remediation is the first capability's: a group is listed most useful
 * first, so for a server with neither history nor aggregates the advice is about
 * history, which is what a raw read — the common call — needed.
 */
export function refusal(
  tool: string,
  decided: Exclude<Verdict, { outcome: "allowed" }>,
  answers: CapabilityAnswers,
  url: string
): string {
  const fields = {
    tool,
    requirement: requirement(decided.capabilities),
    url,
    generation: answers.generation ?? "none",
  };
  if (decided.outcome === "capability_not_supported") {
    return message("capabilityNotSupported", {
      ...fields,
      checked_at: answers.checkedAt ?? "never",
      remediation: spec(decided.capabilities[0]).remediation,
    });
  }
  const unknown = decided.capabilities.find((name) => answers.support[name] !== "not_supported");
  return message("capabilityUnknown", {
    ...fields,
    reason: (unknown && answers.reasons[unknown]) || NOT_YET_ASKED,
  });
}

/** `serverStatus.capabilities`: the cache as it stands, and which session it is from. */
export interface CapabilityStatusRecord {
  session_generation: number | null;
  checked_at: string | null;
  support: Record<string, Support>;
  aggregate_functions: string[];
}

export function capabilityStatus(answers: CapabilityAnswers): CapabilityStatusRecord {
  return {
    session_generation: answers.generation,
    checked_at: answers.checkedAt,
    support: Object.fromEntries(CAPABILITY_NAMES.map((name) => [name, answers.support[name]])),
    aggregate_functions: [...answers.aggregateFunctions],
  };
}
