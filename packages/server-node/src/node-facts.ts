// What a node says about itself: its class, its type, its shape and who may write it.
//
// `node_facts.py` is the Python half and must produce the same record from the
// same address space: the write plan, the method plan, `write_access` and the
// policy check on connect are pure functions of it, driven through both runtimes
// by the shared tables under `tests/fixtures/`, and the tables are only worth
// anything if the record they start from is the same on both.
//
// Every write used to learn one thing about its target — the type of its current
// value — and only by reading it, which is exactly what a write-only node
// refuses. Everything else the plant publishes about a node went unread: that an
// Object has no value, that a node is read-only (AccessLevel) or read-only *for
// this user* (UserAccessLevel), that it holds an array of at most three
// (ValueRank, ArrayDimensions), that its Int32 is an enumeration whose states
// have names, that a method is switched off (Executable). Each of those turned
// into a status code from the plant after the write was sent, or into a value
// written that should not have been.
//
// ## Three logical round trips for a whole batch, then nothing
//
// The same shape as `node-metadata.ts`, for the same reasons. One Read of eight
// attributes for every uncached node; one walk up the supertypes of each
// distinct DataType that is not a built-in one; and, only for the nodes that can
// carry them, one TranslateBrowsePathsToNodeIds and one Read for the properties
// that name an enumeration's states (EnumStrings, EnumValues) and a two-state
// node's labels (TrueState, FalseState). All of it chunked by what the server
// says one request may carry, and all of it cached for the session.
//
// ## The plant model only ever narrows
//
// Nothing here can grant anything. A fact that could not be read is null, and a
// null fact skips its check and leaves the decision to the OPC UA server, which
// enforces its own access rights. That makes this best-effort throughout: a
// failure to read facts is logged and the facts are null, never the reason a
// tool call fails. Failures are not cached; "this server cannot translate browse
// paths" is, for the same reason `NodeMetadata` caches it.
import {
  AttributeIds,
  BrowseDirection,
  ClientSession,
  DataType,
  NodeClass,
  makeBrowsePath,
  type BrowsePathResult,
  type DataValue,
} from "node-opcua-client";

import { browseReferences, supertypeOf } from "./browse.js";
import { isConnectionError } from "./connection.js";
import { CONTRACT } from "./contract.js";
import { chunked } from "./limits.js";
import { canonicalNodeId } from "./node-ids.js";
import {
  UNSTATED,
  readChunk,
  translateChunk,
  type ServerOperationLimits,
} from "./operation-limits.js";
import { isGood } from "./status.js";

/** One state of an enumerated or two-state node: the value written, and its name. */
export interface NodeState {
  value: number;
  label: string;
}

/** What one node said about itself (spec §1.1, and the fixtures' `facts`). */
export interface NodeFacts {
  /** The status of the NodeClass read. Anything but Good means the node is
   *  unknown or unreadable, and every other field is then null. */
  status: string;
  node_class: string | null;
  /** The built-in encoding of the DataType, only when it is one of the fifteen
   *  spellings `write_opcua_nodes` accepts; null for an abstract type. */
  data_type: string | null;
  data_type_id: string | null;
  enumeration: boolean;
  value_rank: number | null;
  array_dimensions: number[] | null;
  access_level: number | null;
  user_access_level: number | null;
  executable: boolean | null;
  user_executable: boolean | null;
  states: NodeState[] | null;
  two_state: { true: string; false: string } | null;
}

/** Whether the node could be inspected at all. */
export function factsGood(facts: NodeFacts | null): facts is NodeFacts {
  return facts !== null && facts.status === "Good";
}

/** AccessLevel bits (Part 3 §8.57). */
export const CURRENT_WRITE = 0x02;
export const HISTORY_READ = 0x04;

/** Whether an AccessLevel is known and lacks `bit`. null — not read — is not a "no". */
export function lacksBit(level: number | null, bit: number): boolean {
  return level !== null && (level & bit) === 0;
}

// --- DataType resolution (spec §1.3) ------------------------------------------

/** What a DataType is written as on the wire, for a write. */
export interface DataTypeResolution {
  data_type: string | null;
  enumeration: boolean;
}

const UNRESOLVED: DataTypeResolution = { data_type: null, enumeration: false };

/** The fifteen data_type spellings `write_opcua_nodes` accepts, read from the
 *  tool's own input schema so the two can never list different types. */
const WRITABLE_TYPES: ReadonlySet<string> = new Set(
  CONTRACT.tools.find((tool) => tool.name === "write_opcua_nodes")?.inputSchema?.properties?.nodes
    ?.items?.properties?.data_type?.enum ?? []
);

/** Enumeration (Part 6 §5.2.4): every enumerated DataType is an Int32 on the wire. */
const ENUMERATION = 29;
/** Structure, DataValue, BaseDataType, DiagnosticInfo, Number, Integer, UInteger:
 *  reaching one of these means the type is abstract, or not one a write converts
 *  to, and the walk stops with no answer. */
const ABSTRACT_FIRST = 22;
const ABSTRACT_LAST = 28;
/** ns=0 identifiers 1-21 are the concrete built-in types (Part 6 §5.1.2). */
const BUILT_IN_LAST = 21;
/** Far deeper than any real type hierarchy; bounds a server whose HasSubtype
 *  references form a loop, as `method-arguments.ts` does. */
const MAX_TYPE_DEPTH = 32;
const NS0_NUMERIC = /^ns=0;i=([0-9]+)$/;

/** What a declared DataType is written as, walking up its supertypes.
 *
 * Pure but for `supertypeOf`, which the live cache answers with a browse and the
 * shared table (`data-type-resolution.json`) with a map. Never throws: a type
 * that cannot be resolved is a check that is skipped, not a write that is
 * refused, so a dead end, a loop, an abstract type and a failing lookup all come
 * back as "no type known" — and the write then converts to the type of the
 * node's current value, as it always did.
 */
export async function resolveDataType(
  dataTypeId: string,
  supertypeOf: (dataType: string) => Promise<string | null> | string | null
): Promise<DataTypeResolution> {
  const seen = new Set<string>();
  let current: string | null = canonicalNodeId(dataTypeId);
  try {
    for (let depth = 0; depth < MAX_TYPE_DEPTH && current !== null; depth++) {
      if (seen.has(current)) return UNRESOLVED;
      seen.add(current);
      const match = NS0_NUMERIC.exec(current);
      if (match) {
        const identifier = Number(match[1]);
        if (identifier === ENUMERATION) return { data_type: "Int32", enumeration: true };
        if (identifier >= 1 && identifier <= BUILT_IN_LAST) {
          const name = DataType[identifier];
          return { data_type: WRITABLE_TYPES.has(name) ? name : null, enumeration: false };
        }
        if (identifier >= ABSTRACT_FIRST && identifier <= ABSTRACT_LAST) return UNRESOLVED;
      }
      const parent: string | null = await supertypeOf(current);
      current = parent === null ? null : canonicalNodeId(parent);
    }
  } catch {
    return UNRESOLVED;
  }
  return UNRESOLVED;
}

// --- reading ------------------------------------------------------------------

/** Round 1, in the order the eight values come back for each node. */
const ATTRIBUTES = [
  AttributeIds.NodeClass,
  AttributeIds.DataType,
  AttributeIds.ValueRank,
  AttributeIds.ArrayDimensions,
  AttributeIds.AccessLevel,
  AttributeIds.UserAccessLevel,
  AttributeIds.Executable,
  AttributeIds.UserExecutable,
] as const;

/** The types whose values a state can name: Boolean (TrueState/FalseState), the
 *  integers (EnumStrings on a MultiStateDiscrete). An enumeration DataType is
 *  asked about whatever its encoding. */
const STATE_TYPES = new Set([
  "Boolean",
  "SByte",
  "Byte",
  "Int16",
  "UInt16",
  "Int32",
  "UInt32",
  "Int64",
  "UInt64",
]);

const VARIABLE_PROPERTIES = ["0:EnumStrings", "0:EnumValues", "0:TrueState", "0:FalseState"];
const DATA_TYPE_PROPERTIES = ["0:EnumStrings", "0:EnumValues"];

/** How many browse paths or reads one request may carry; the same project cap
 *  as the engineering-unit properties, for the same reason. */
const MAX_PER_REQUEST = CONTRACT.analog.maxPropertiesPerRequest;

/** HasTypeDefinition, which names the type whose methods an object also has. */
const HAS_TYPE_DEFINITION = 40;
/** How far up an object's type hierarchy a method is looked for. */
const MAX_METHOD_TYPE_DEPTH = 8;

function unknownFacts(status: string): NodeFacts {
  return {
    status,
    node_class: null,
    data_type: null,
    data_type_id: null,
    enumeration: false,
    value_rank: null,
    array_dimensions: null,
    access_level: null,
    user_access_level: null,
    executable: null,
    user_executable: null,
    states: null,
    two_state: null,
  };
}

/** A Good attribute's value, or undefined: a Bad attribute is a fact not known. */
function goodValue(dataValue: DataValue | undefined): unknown {
  if (!dataValue || !isGood(dataValue.statusCode)) return undefined;
  return dataValue.value?.value;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** An ArrayDimensions value — a typed array from node-opcua — or null when empty. */
function dimensions(value: unknown): number[] | null {
  if (value === null || value === undefined || typeof value !== "object") return null;
  const list = Array.from(value as ArrayLike<unknown>).filter(
    (item): item is number => typeof item === "number"
  );
  return list.length > 0 ? list : null;
}

/** One node's eight attributes as a facts record, before its type is resolved. */
function fromAttributes(values: Array<DataValue | undefined>): NodeFacts {
  const [nodeClass, dataType, valueRank, arrayDimensions, access, userAccess, exec, userExec] =
    values;
  if (!nodeClass || !isGood(nodeClass.statusCode)) {
    return unknownFacts(nodeClass?.statusCode?.name ?? "BadUnexpectedError");
  }
  const classValue = goodValue(nodeClass);
  const typeId = goodValue(dataType) as { toString(): string } | null | undefined;
  return {
    ...unknownFacts("Good"),
    node_class: typeof classValue === "number" ? (NodeClass[classValue] ?? null) : null,
    data_type_id: typeId ? canonicalNodeId(typeId.toString()) : null,
    value_rank: numberOrNull(goodValue(valueRank)),
    array_dimensions: dimensions(goodValue(arrayDimensions)),
    access_level: numberOrNull(goodValue(access)),
    user_access_level: numberOrNull(goodValue(userAccess)),
    executable: booleanOrNull(goodValue(exec)),
    user_executable: booleanOrNull(goodValue(userExec)),
  };
}

/** The readable half of a LocalizedText, or null if it carries none. */
function localizedText(value: unknown): string | null {
  const text = (value as { text?: unknown } | null)?.text;
  return typeof text === "string" && text.length > 0 ? text : null;
}

/** An Int64 as a number. node-opcua decodes one as `[high, low]`. */
function int64Number(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "bigint") return Number(value);
  if (Array.isArray(value) && value.length === 2) {
    const [high, low] = value;
    if (typeof high === "number" && typeof low === "number") return high * 2 ** 32 + low;
  }
  return null;
}

/** EnumStrings (LocalizedText[]): each label's value is its index. */
function statesFromEnumStrings(value: unknown): NodeState[] | null {
  if (!Array.isArray(value)) return null;
  const states: NodeState[] = [];
  value.forEach((item, index) => {
    const label = localizedText(item);
    if (label !== null) states.push({ value: index, label });
  });
  return states.length > 0 ? states : null;
}

/** EnumValues (EnumValueType[]): each carries its own value, so gaps are allowed. */
function statesFromEnumValues(value: unknown): NodeState[] | null {
  if (!Array.isArray(value)) return null;
  const states: NodeState[] = [];
  for (const item of value) {
    const entry = item as { value?: unknown; displayName?: unknown } | null;
    const number = int64Number(entry?.value);
    const label = localizedText(entry?.displayName);
    if (number !== null && label !== null) states.push({ value: number, label });
  }
  return states.length > 0 ? states : null;
}

/** EnumStrings first, then EnumValues: a node publishes one or the other. */
function statesFrom(enumStrings: unknown, enumValues: unknown): NodeState[] | null {
  return statesFromEnumStrings(enumStrings) ?? statesFromEnumValues(enumValues);
}

/** The NodeId a browse path resolved to, or null if it resolved to nothing. */
function firstTarget(result: BrowsePathResult): string | null {
  if (!isGood(result.statusCode)) return null;
  const target = result.targets?.[0];
  return target ? target.targetId.toString() : null;
}

/** Whether `methodId` is a method of `objectId`, or of its type or a supertype.
 *
 * A method is called on an object, and a server runs it only where it belongs:
 * among the object's own references, or its type's (Part 4 §5.11.2). True if
 * found, false when every browse answered and it was not, null when a browse
 * failed — "could not tell" skips the check rather than refusing.
 */
async function methodOnObject(
  session: ClientSession,
  objectId: string,
  methodId: string
): Promise<boolean | null> {
  const forward = (nodeId: string) =>
    browseReferences(session, {
      nodeId,
      browseDirection: BrowseDirection.Forward,
      includeSubtypes: true,
      nodeClassMask: 0,
      resultMask: 63,
    });
  const holds = (references: Awaited<ReturnType<typeof forward>>) =>
    references.some((reference) => canonicalNodeId(reference.nodeId.toString()) === methodId);

  const own = await forward(objectId);
  if (holds(own)) return true;
  const typeReference = own.find(
    (reference) =>
      reference.referenceTypeId.namespace === 0 &&
      reference.referenceTypeId.value === HAS_TYPE_DEFINITION
  );
  let type: string | null = typeReference ? canonicalNodeId(typeReference.nodeId.toString()) : null;
  const seen = new Set<string>();
  for (let depth = 0; depth < MAX_METHOD_TYPE_DEPTH && type !== null; depth++) {
    if (seen.has(type)) break;
    seen.add(type);
    if (holds(await forward(type))) return true;
    type = await supertypeOf(session, type);
  }
  return false;
}

/** The per-session cache of what each node said about itself. */
export class NodeFactsCache {
  private readonly cache = new Map<string, NodeFacts>();
  /** DataType id → what it is written as, and its own states when it is an
   *  enumeration that publishes them; read once per session per type. */
  private readonly dataTypes = new Map<string, DataTypeResolution>();
  private readonly dataTypeStates = new Map<string, NodeState[] | null>();
  /** (object, method) → whether the method is the object's. */
  private readonly methodsOnObjects = new Map<string, boolean>();
  /** Set when the server cannot answer TranslateBrowsePaths at all; see
   *  `NodeMetadata.unanswerable`. States and labels are then never known. */
  private unanswerable = false;
  /** What the connected server says one request may carry. Set by the
   *  capability probe on every session. */
  serverLimits: ServerOperationLimits = UNSTATED;

  /** Drop everything, because the session it was true of is gone. */
  forget(): void {
    this.cache.clear();
    this.dataTypes.clear();
    this.dataTypeStates.clear();
    this.methodsOnObjects.clear();
    this.unanswerable = false;
  }

  /** What is already known about `nodeId`, without asking the server. */
  cached(nodeId: string): NodeFacts | null {
    return this.cache.get(nodeId) ?? null;
  }

  /** What each node says about itself, reading only the ones not already known.
   *
   * Null for a node whose facts could not be read at all — which every caller
   * treats as "nothing known, let the server decide". Never throws.
   */
  async forNodes(
    session: ClientSession,
    nodeIds: string[]
  ): Promise<Map<string, NodeFacts | null>> {
    const wanted = [...new Set(nodeIds)];
    const missing = wanted.filter((nodeId) => !this.cache.has(nodeId));
    const found = new Map<string, NodeFacts>();
    if (missing.length > 0) {
      try {
        const { facts, complete } = await this.read(session, missing);
        for (const [nodeId, record] of facts) {
          found.set(nodeId, record);
          // A record whose states could not be read this time is returned but
          // not kept: the next call asks again, as a failure is never cached.
          if (complete.has(nodeId)) this.cache.set(nodeId, record);
        }
      } catch (error) {
        console.error(
          `Could not read node attributes: ${error instanceof Error ? error.message : String(error)}`
        );
      }
    }
    return new Map(
      wanted.map((nodeId) => [nodeId, this.cache.get(nodeId) ?? found.get(nodeId) ?? null])
    );
  }

  /** Whether `methodId` is a method of `objectId` or its type; null if unknown.
   *
   * Both ids canonical. Cached per session once answered; a failure is not.
   */
  async onObject(
    session: ClientSession,
    objectId: string,
    methodId: string
  ): Promise<boolean | null> {
    const key = `${objectId}|${methodId}`;
    const known = this.methodsOnObjects.get(key);
    if (known !== undefined) return known;
    try {
      const answer = await methodOnObject(session, objectId, methodId);
      if (answer !== null) this.methodsOnObjects.set(key, answer);
      return answer;
    } catch (error) {
      console.error(
        `Could not tell whether ${methodId} is a method of ${objectId}: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
      return null;
    }
  }

  /** The three rounds for the nodes not cached. See the file header. */
  private async read(
    session: ClientSession,
    nodeIds: string[]
  ): Promise<{ facts: Map<string, NodeFacts>; complete: Set<string> }> {
    // Round 1: eight attributes per node, in one chunked Read.
    const items = nodeIds.flatMap((nodeId) =>
      ATTRIBUTES.map((attributeId) => ({ nodeId, attributeId }))
    );
    const values: DataValue[] = [];
    for (const part of chunked(items, readChunk(this.serverLimits))) {
      values.push(...(await session.read(part)));
    }
    const facts = new Map<string, NodeFacts>();
    nodeIds.forEach((nodeId, index) => {
      const start = index * ATTRIBUTES.length;
      facts.set(nodeId, fromAttributes(values.slice(start, start + ATTRIBUTES.length)));
    });
    const complete = new Set(nodeIds);

    // Round 2: what each distinct DataType is written as.
    for (const typeId of new Set([...facts.values()].map((record) => record.data_type_id))) {
      if (typeId === null || this.dataTypes.has(typeId)) continue;
      let failed = false;
      const resolution = await resolveDataType(typeId, async (child) => {
        try {
          return await supertypeOf(session, child);
        } catch (error) {
          failed = true;
          throw error;
        }
      });
      if (failed) {
        // Used for this call, not remembered: a browse that failed is not an
        // answer about the type.
        for (const [nodeId, record] of facts) {
          if (record.data_type_id === typeId) {
            Object.assign(record, resolution);
            complete.delete(nodeId);
          }
        }
        continue;
      }
      this.dataTypes.set(typeId, resolution);
    }
    for (const record of facts.values()) {
      const resolution = record.data_type_id ? this.dataTypes.get(record.data_type_id) : undefined;
      if (resolution) Object.assign(record, resolution);
    }

    // Round 3: the properties that name states, for the nodes that can have them.
    const askVariables = [...facts].filter(
      ([, record]) =>
        record.node_class === "Variable" &&
        (record.enumeration || (record.data_type !== null && STATE_TYPES.has(record.data_type)))
    );
    const askTypes = [
      ...new Set(
        askVariables
          .map(([, record]) => record)
          .filter((record) => record.enumeration && record.data_type_id !== null)
          .map((record) => record.data_type_id as string)
      ),
    ].filter((typeId) => !this.dataTypeStates.has(typeId));
    if (askVariables.length === 0 || this.unanswerable) return { facts, complete };

    const paths = [
      ...askVariables.flatMap(([nodeId]) =>
        VARIABLE_PROPERTIES.map((name) => makeBrowsePath(nodeId, `/${name}`))
      ),
      ...askTypes.flatMap((typeId) =>
        DATA_TYPE_PROPERTIES.map((name) => makeBrowsePath(typeId, `/${name}`))
      ),
    ];
    let properties: unknown[];
    try {
      properties = await this.readProperties(session, paths);
    } catch (error) {
      // The same rule as NodeMetadata: a server that cannot translate will not
      // learn to; a connection that died is not an answer.
      this.unanswerable = !isConnectionError(error);
      console.error(
        `Could not read enumeration states and labels: ${
          error instanceof Error ? error.message : String(error)
        }${this.unanswerable ? "; not asking this session again" : ""}`
      );
      if (!this.unanswerable) {
        for (const [nodeId] of askVariables) complete.delete(nodeId);
      }
      return { facts, complete };
    }

    const typeOffset = askVariables.length * VARIABLE_PROPERTIES.length;
    askTypes.forEach((typeId, index) => {
      const at = typeOffset + index * DATA_TYPE_PROPERTIES.length;
      this.dataTypeStates.set(typeId, statesFrom(properties[at], properties[at + 1]));
    });
    askVariables.forEach(([, record], index) => {
      const at = index * VARIABLE_PROPERTIES.length;
      const [enumStrings, enumValues, trueState, falseState] = properties.slice(
        at,
        at + VARIABLE_PROPERTIES.length
      );
      // The variable's own list wins: a MultiStateDiscrete names its states on
      // itself, and a node may narrow what its DataType says.
      record.states =
        statesFrom(enumStrings, enumValues) ??
        (record.enumeration && record.data_type_id !== null
          ? (this.dataTypeStates.get(record.data_type_id) ?? null)
          : null);
      const whenTrue = localizedText(trueState);
      const whenFalse = localizedText(falseState);
      record.two_state =
        whenTrue !== null && whenFalse !== null ? { true: whenTrue, false: whenFalse } : null;
    });
    return { facts, complete };
  }

  /** One translate and one read for every path; null where a path resolved to
   *  nothing or its value was not Good. */
  private async readProperties(
    session: ClientSession,
    paths: ReturnType<typeof makeBrowsePath>[]
  ): Promise<unknown[]> {
    const results: BrowsePathResult[] = [];
    for (const part of chunked(paths, translateChunk(this.serverLimits, MAX_PER_REQUEST))) {
      results.push(...(await session.translateBrowsePath(part)));
    }
    const targets: string[] = [];
    const owners: number[] = [];
    results.forEach((result, index) => {
      const target = firstTarget(result);
      if (target === null) return;
      targets.push(target);
      owners.push(index);
    });
    const values: DataValue[] = [];
    for (const part of chunked(targets, readChunk(this.serverLimits, MAX_PER_REQUEST))) {
      values.push(
        ...(await session.read(part.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value }))))
      );
    }
    const properties: unknown[] = new Array(paths.length).fill(null);
    owners.forEach((pathIndex, position) => {
      const value = goodValue(values[position]);
      if (value !== undefined) properties[pathIndex] = value;
    });
    return properties;
  }
}
