// What a write to one node will be, decided from the node's own attributes.
//
// `write_plan.py` is the Python half. Both are pure, and both are driven through
// the same two tables — `tests/fixtures/write-plan.json` for `planWrite`,
// `tests/fixtures/write-access.json` for `writeAccess` — because a write that one
// runtime sends and the other skips is a plant that moved or did not depending
// on which server the operator happened to start.
//
// The write path used to know one thing about its target: the type of its
// current value, learned by reading it. A plan is decided from everything the
// node publishes (node-facts.ts) before anything is sent, and comes out as one of
// three things:
//
// - `send`: convert and write, with the type and array-ness worked out here — or
//   null for either, which keeps the old read-first behaviour for whatever the
//   node does not say.
// - `skip`: this node cannot take this write whoever asks — an Object, a
//   read-only node, a list for a scalar. One record with the status the plant
//   would have answered, nothing sent to it, the rest of the batch unaffected,
//   exactly as a value that will not convert already was.
// - `refuse`: the value is not one the node can mean — a state it does not
//   define, a number outside the range the plant published. The whole batch is
//   refused and nothing written, which is what a value bound already did.
//
// A fact that could not be read skips its check and leaves the decision to the
// OPC UA server. Nothing here can make a write possible that was not; it can only
// stop one earlier, with a better sentence.
import { message, sentence } from "./errors.js";
import {
  factsGood,
  lacksBit,
  CURRENT_WRITE,
  type NodeFacts,
  type NodeState,
} from "./node-facts.js";
import { withinRange, type AnalogInfo, type Range } from "./node-metadata.js";
import { numericText, parseNumericString } from "./numeric.js";
import { asNumber, formatNumber } from "./policy.js";

/** One write as `write_opcua_nodes` received it. */
export interface WriteRequestLike {
  node_id: string;
  value: unknown;
  data_type?: string | null;
}

export type WritePlan =
  | { outcome: "send"; data_type: string | null; array: boolean | null; value: unknown }
  | { outcome: "skip"; status: string; error: string }
  | { outcome: "refuse"; error: string };

/** Where an EURange refusal says its bound came from. Existing wording. */
export const EU_RANGE_SOURCE = "the OPC UA server's own EURange";
/** And an InstrumentRange one. */
export const INSTRUMENT_RANGE_SOURCE = "the instrument's own InstrumentRange";

/** A value in a sentence: a number as `formatNumber` writes it, anything else as
 *  JSON — so `100`, `"AUTO"` and `true` read the same on both runtimes. */
export function valueText(value: unknown): string {
  if (typeof value === "number") return formatNumber(value);
  return JSON.stringify(value) ?? String(value);
}

/** A node's states as a refusal lists them: `0 = Running, 1 = Failed`. */
export function statesText(states: readonly NodeState[]): string {
  return states.map((state) => `${formatNumber(state.value)} = ${state.label}`).join(", ");
}

/** The refusal for a number outside `range`, or null when it is inside. */
export function outOfRangeMessage(
  nodeId: string,
  number: number,
  range: Range,
  unit: string | null,
  source: string
): string | null {
  if (withinRange(range, number)) return null;
  return message("valueOutOfRange", {
    value: formatNumber(number),
    node_id: nodeId,
    low: formatNumber(range.low),
    high: formatNumber(range.high),
    unit: unit ? ` ${unit}` : "",
    source,
  });
}

function skip(status: string, key: string, fields: Record<string, string | number>): WritePlan {
  return { outcome: "skip", status, error: sentence("writeSkips", key, fields) };
}

/** One element of a write to an enumerated or two-state node, as written.
 *
 * A state may be named by its label, exactly as the server lists it, which is
 * what an agent reads off the HMI; or by its number, which must be one the node
 * defines. Anything else is not a state, and is refused rather than passed to
 * the codec — an Int32 enumeration accepts 9 as readily as 3, and the plant then
 * holds a state nobody defined.
 */
function resolveState(element: unknown, states: readonly NodeState[]): { value: number } | null {
  if (typeof element === "string" && numericText(element) === null) {
    const state = states.find((candidate) => candidate.label === element);
    return state ? { value: state.value } : null;
  }
  const number =
    typeof element === "number"
      ? element
      : typeof element === "string"
        ? parseNumericString(element)
        : null;
  if (number === null || !Number.isInteger(number)) return null;
  return states.some((state) => state.value === number) ? { value: number } : null;
}

/** The plan for one write (spec §2). Pure; see the file header.
 *
 * Steps in order, the first that applies deciding: nothing known; not a
 * Variable; not writable; not writable for this user; a contradicting data_type;
 * the wrong shape; a value that is not a state; a value outside the plant's own
 * ranges. `tests/fixtures/write-plan.json` pins every one.
 */
export function planWrite(
  request: WriteRequestLike,
  facts: NodeFacts | null,
  engineering: AnalogInfo | null,
  options: { allow_out_of_range: boolean }
): WritePlan {
  const nodeId = request.node_id;
  const requested = request.data_type ?? null;
  const value = request.value;

  // Nothing known: the server answers, exactly as it did before any of this.
  if (!factsGood(facts)) {
    return { outcome: "send", data_type: requested, array: null, value };
  }
  if (facts.node_class !== "Variable") {
    return skip("BadNodeClassInvalid", "notVariable", {
      node_id: nodeId,
      node_class: facts.node_class ?? "unknown",
    });
  }
  if (lacksBit(facts.access_level, CURRENT_WRITE)) {
    return skip("BadNotWritable", "notWritable", { node_id: nodeId });
  }
  if (lacksBit(facts.user_access_level, CURRENT_WRITE)) {
    return skip("BadUserAccessDenied", "notWritableForUser", { node_id: nodeId });
  }

  // The type. An explicit data_type that contradicts a concrete DataType would
  // be converted to the wrong encoding and refused by the server, or worse,
  // accepted; one for an abstract DataType is exactly what data_type is for.
  if (requested !== null && facts.data_type !== null && requested !== facts.data_type) {
    return skip("BadTypeMismatch", "typeMismatch", {
      node_id: nodeId,
      node_type: facts.data_type,
      data_type: requested,
    });
  }
  let type: string | null = requested ?? facts.data_type;

  // The shape.
  const isList = Array.isArray(value);
  const rank = facts.value_rank;
  if (rank === -1 && isList) {
    return skip("BadTypeMismatch", "needsScalar", { node_id: nodeId });
  }
  if (rank !== null && rank >= 0 && !isList) {
    return skip("BadTypeMismatch", "needsArray", { node_id: nodeId, value_rank: rank });
  }
  const limit = facts.array_dimensions?.[0] ?? 0;
  if (rank === 1 && limit > 0 && isList && value.length > limit) {
    return skip("BadOutOfRange", "arrayTooLong", {
      node_id: nodeId,
      limit,
      count: value.length,
    });
  }
  let array: boolean | null;
  if (rank === null) array = null;
  else if (rank === -1) array = false;
  else if (rank === 0 || rank === 1) array = true;
  else if (rank >= 2) {
    // Multi-dimensional: the dimensions can only come from the current value,
    // so the type does too unless the caller named it, as before.
    array = requested === null ? null : true;
    if (requested === null) type = null;
  } else array = isList; // -2 Any, -3 ScalarOrOneDimension: whatever was sent

  // States, element by element.
  const elements: unknown[] = isList ? value : [value];
  let resolved: unknown[] = elements;
  if (facts.states !== null) {
    const states = facts.states;
    resolved = [];
    for (const element of elements) {
      const state = resolveState(element, states);
      if (state === null) {
        return {
          outcome: "refuse",
          error: message("valueNotAState", {
            value: JSON.stringify(element) ?? String(element),
            node_id: nodeId,
            allowed: statesText(states),
          }),
        };
      }
      resolved.push(state.value);
    }
  } else if (facts.two_state !== null) {
    const labels = facts.two_state;
    resolved = elements.map((element) =>
      element === labels.true ? true : element === labels.false ? false : element
    );
  }

  // The plant's own ranges. The EURange is normal operation, and the override
  // is for writing outside it on purpose; the InstrumentRange is what the device
  // can represent, and nothing writes past that on purpose.
  const unit = engineering?.unit ?? null;
  for (const element of resolved) {
    const number = asNumber(element);
    if (number === null) continue;
    const euRange = options.allow_out_of_range ? null : (engineering?.eu_range ?? null);
    const refusal =
      (euRange && outOfRangeMessage(nodeId, number, euRange, unit, EU_RANGE_SOURCE)) ||
      (engineering?.instrument_range &&
        outOfRangeMessage(
          nodeId,
          number,
          engineering.instrument_range,
          unit,
          INSTRUMENT_RANGE_SOURCE
        ));
    if (refusal) return { outcome: "refuse", error: refusal };
  }

  return { outcome: "send", data_type: type, array, value: isList ? resolved : resolved[0] };
}

// --- write_access (spec §5) -----------------------------------------------------

/** An operator value bound as the fixtures spell it. */
export interface BoundRecord {
  min: number | null;
  max: number | null;
  enum: readonly unknown[] | null;
  max_change: number | null;
}

/** What `writeAccess` is told about the policy, for one node. */
export interface WriteAccessPolicy {
  /** Why write_opcua_nodes is not callable at all, or null if it is. */
  tool_refusal: string | null;
  operator: boolean;
  /** Whether the node is in the operator's writable set. */
  allowlisted: boolean;
  bound: BoundRecord | null;
  allow_out_of_range: boolean;
}

/** `resultShapes.nodeValues.items.properties.write_access`. */
export interface WriteAccessRecord {
  allowed: boolean;
  reason: string | null;
  data_type: string | null;
  array: boolean | null;
  states: Array<{ value: number | boolean; label: string }> | null;
  min: number | null;
  max: number | null;
  allowed_values: readonly unknown[] | null;
  max_change: number | null;
}

/** Whether a node can be written, and what a write must look like (spec §5).
 *
 * The same facts and the same policy the write path decides on, reported before
 * trying, so an agent gets a write right the first time instead of learning the
 * rules one refusal at a time. The reason is the first that applies, in the
 * order the write path would meet them — the node, then the policy, then the
 * server's access levels — and a policy refusal is quoted verbatim. Pure;
 * `tests/fixtures/write-access.json` pins it.
 */
export function writeAccess(input: {
  node_id: string;
  facts: NodeFacts | null;
  engineering: AnalogInfo | null;
  policy: WriteAccessPolicy;
}): WriteAccessRecord {
  const { node_id: nodeId, facts, engineering, policy } = input;
  let reason: string | null = null;
  if (facts === null) {
    reason = sentence("writeAccessReasons", "unknown");
  } else if (facts.status !== "Good") {
    reason = sentence("writeAccessReasons", "unreadable", { status: facts.status });
  } else if (facts.node_class !== "Variable") {
    reason = sentence("writeAccessReasons", "notVariable", {
      node_class: facts.node_class ?? "unknown",
    });
  } else if (policy.tool_refusal !== null) {
    reason = policy.tool_refusal;
  } else if (policy.operator && !policy.allowlisted) {
    reason = message("nodeNotWritable", { node_id: nodeId });
  } else if (lacksBit(facts.access_level, CURRENT_WRITE)) {
    reason = sentence("writeAccessReasons", "readOnly");
  } else if (lacksBit(facts.user_access_level, CURRENT_WRITE)) {
    reason = sentence("writeAccessReasons", "readOnlyForUser");
  }

  const known = factsGood(facts) ? facts : null;
  const rank = known?.value_rank ?? null;
  const states: WriteAccessRecord["states"] =
    known?.states ??
    (known?.two_state
      ? [
          { value: true, label: known.two_state.true },
          { value: false, label: known.two_state.false },
        ]
      : null);

  // The tightest of everything that bounds a value, each only where it applies.
  const bound = policy.operator ? policy.bound : null;
  const euRange = policy.allow_out_of_range ? null : (engineering?.eu_range ?? null);
  const instrumentRange = engineering?.instrument_range ?? null;
  const lows = [euRange?.low, instrumentRange?.low, bound?.min].filter(isNumber);
  const highs = [euRange?.high, instrumentRange?.high, bound?.max].filter(isNumber);

  return {
    allowed: reason === null,
    reason,
    data_type: known?.data_type ?? null,
    array: rank === null ? null : rank === -1 ? false : rank >= 0 ? true : null,
    states,
    min: lows.length > 0 ? Math.max(...lows) : null,
    max: highs.length > 0 ? Math.min(...highs) : null,
    allowed_values: bound?.enum ?? null,
    max_change: bound?.max_change ?? null,
  };
}

function isNumber(value: number | null | undefined): value is number {
  return typeof value === "number";
}
