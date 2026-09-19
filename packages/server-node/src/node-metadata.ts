// What a node says its number means: the unit, and the ranges around it.
//
// `node_metadata.py` is the Python half and must produce the same records from
// the same address space, because `resultShapes.nodeValues` -> `engineering` is
// one shape and a client that learns one server's output has to be able to read
// the other's.
//
// A bare `51.75` cannot be told from °C, PSI or %, and cannot be told from a
// trip. OPC UA Part 8 §5.3 introduces `EngineeringUnits` by citing the Mars
// Climate Orbiter, and defines `EURange` as the range a value is expected to
// occupy in normal operation and `InstrumentRange` as what the instrument can
// physically return. All three are standard properties of `AnalogItemType`,
// which is what a real PLC or SCADA server exposes an analogue tag as — so this
// is information the plant has already published and nobody was reading.
//
// ## Two round trips for a whole batch, then nothing
//
// Resolving three properties per node by browsing would be three round trips per
// node, which would make a 500-node read unusable. Instead every property of
// every uncached node goes into one `TranslateBrowsePathsToNodeIds`, and
// everything that resolved goes into one `Read`. So a cold call costs two extra
// round trips whatever the batch size, and a warm one costs none. (Two
// *logical* round trips: both are chunked, because 500 nodes is 1500 browse
// paths and `MaxNodesPerTranslateBrowsePathsToNodeIds` is an operational limit
// a conformant server may enforce.)
//
// The cache is per session and is dropped when the session is replaced
// (`forget`): a node's unit does not change while a session lasts, and a server
// that has been restarted may not be the same server.
//
// A failure is not cached, with one exception. "This node has no EURange" is an
// answer worth remembering; "the connection died while we asked" is not, and
// remembering it would leave a whole session's readings unqualified because of
// one bad moment. But a server that cannot answer the *question* — one that does
// not implement TranslateBrowsePathsToNodeIds — will not learn to, and asking it
// again on every read would cost a round trip and a line of stderr each time. So
// that answer is remembered, for the session.
import {
  AttributeIds,
  ClientSession,
  StatusCodes,
  makeBrowsePath,
  type BrowsePathResult,
  type DataValue,
} from "node-opcua-client";

import { isConnectionError } from "./connection.js";
import { CONTRACT } from "./contract.js";

const ANALOG = CONTRACT.analog;

/** How many browse paths or node reads one request may carry.
 *
 * A 500-node read is 1500 browse paths, and
 * `MaxNodesPerTranslateBrowsePathsToNodeIds` is an operational limit servers
 * publish and enforce — so the batch is chunked rather than sent as one request
 * a conformant server is entitled to refuse.
 */
const MAX_PER_REQUEST = ANALOG.maxPropertiesPerRequest;

/** `items` in requests no larger than the server is obliged to accept. */
function chunked<T>(items: T[]): T[][] {
  const chunks: T[][] = [];
  for (let start = 0; start < items.length; start += MAX_PER_REQUEST) {
    chunks.push(items.slice(start, start + MAX_PER_REQUEST));
  }
  return chunks;
}

/** The three properties, in the order their browse paths are built and read. */
export const PROPERTY_BROWSE_NAMES = [
  ANALOG.engineeringUnitsBrowseName,
  ANALOG.euRangeBrowseName,
  ANALOG.instrumentRangeBrowseName,
] as const;

/** One `Range` structure: what the two numbers in it are called here. */
export interface Range {
  low: number;
  high: number;
}

/** What one node published about its own number. */
export interface AnalogInfo {
  unit: string | null;
  unit_description: string | null;
  eu_range: Range | null;
  instrument_range: Range | null;
}

/** Inclusive on both ends, as OPC UA's Range is. */
export function withinRange(range: Range, value: number): boolean {
  return value >= range.low && value <= range.high;
}

/** The readable half of a `LocalizedText`, or null if it carries none. */
function localizedText(value: unknown): string | null {
  const text = (value as { text?: unknown } | null)?.text;
  return typeof text === "string" && text.length > 0 ? text : null;
}

/** A `Range` structure as this module's record, or null if it is not one. */
function toRange(value: unknown): Range | null {
  const candidate = value as { low?: unknown; high?: unknown } | null | undefined;
  const low = Number(candidate?.low);
  const high = Number(candidate?.high);
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
  return { low, high };
}

/** One node's three property values as a record, or null if it published none. */
function assemble(properties: unknown[]): AnalogInfo | null {
  const [units, euRange, instrumentRange] = properties;
  const info: AnalogInfo = {
    unit: localizedText((units as { displayName?: unknown } | null)?.displayName),
    unit_description: localizedText((units as { description?: unknown } | null)?.description),
    eu_range: toRange(euRange),
    instrument_range: toRange(instrumentRange),
  };
  const empty =
    info.unit === null &&
    info.unit_description === null &&
    info.eu_range === null &&
    info.instrument_range === null;
  return empty ? null : info;
}

/** The NodeId a browse path resolved to, or null if it resolved to nothing.
 *
 * A node with no EURange answers `BadNoMatch`, which is an answer and not a
 * failure: most nodes are not AnalogItems.
 */
function firstTarget(result: BrowsePathResult): string | null {
  if (result.statusCode !== StatusCodes.Good) return null;
  const target = result.targets?.[0];
  return target ? target.targetId.toString() : null;
}

/** The per-session cache of what each node said about its own number. */
export class NodeMetadata {
  private readonly cache = new Map<string, AnalogInfo | null>();
  /** Set when this server answered "no" to the question itself rather than to one
   *  node — a server that does not implement TranslateBrowsePathsToNodeIds, say.
   *  Without it every read would pay a failed round trip and write a line to
   *  stderr, forever. */
  private unanswerable = false;

  /** Drop everything, because the session it was true of is gone. */
  forget(): void {
    this.cache.clear();
    this.unanswerable = false;
  }

  /** What is already known about `nodeId`, without asking the server. */
  cached(nodeId: string): AnalogInfo | null {
    return this.cache.get(nodeId) ?? null;
  }

  /** What each node publishes, reading only the ones not already known.
   *
   * Best-effort throughout: a node that publishes nothing, a server that does
   * not implement TranslateBrowsePaths, and a read that comes back Bad all
   * produce `null` for that node rather than an error. A reading without its
   * unit is worth less than one with it and is still worth returning — this must
   * never be the reason a read fails.
   */
  async forNodes(
    session: ClientSession,
    nodeIds: string[]
  ): Promise<Map<string, AnalogInfo | null>> {
    const wanted = [...new Set(nodeIds)];
    const missing = wanted.filter((nodeId) => !this.cache.has(nodeId));
    if (missing.length > 0 && !this.unanswerable) {
      try {
        for (const [nodeId, info] of await this.read(session, missing)) {
          this.cache.set(nodeId, info);
        }
      } catch (error) {
        // A connection error is not an answer about the address space, and
        // remembering it as one would leave a whole session's readings
        // unqualified because of one bad moment. Anything else *is* an answer —
        // a server that cannot translate browse paths will not learn to — and
        // asking it again every read would cost a round trip and a line of
        // stderr each time.
        this.unanswerable = !isConnectionError(error);
        console.error(
          `Could not read engineering units and ranges: ${
            error instanceof Error ? error.message : String(error)
          }${this.unanswerable ? "; not asking this session again" : ""}`
        );
      }
    }
    return new Map(wanted.map((nodeId) => [nodeId, this.cache.get(nodeId) ?? null]));
  }

  /** One translate and one read for the whole batch. See the file header. */
  private async read(
    session: ClientSession,
    nodeIds: string[]
  ): Promise<Map<string, AnalogInfo | null>> {
    const paths = nodeIds.flatMap((nodeId) =>
      PROPERTY_BROWSE_NAMES.map((browseName) => makeBrowsePath(nodeId, `/${browseName}`))
    );
    const results: BrowsePathResult[] = [];
    for (const chunk of chunked(paths)) {
      results.push(...(await session.translateBrowsePath(chunk)));
    }

    // Which property of which node each resolved target belongs to, so the one
    // flat read below can be put back into records.
    const targets: string[] = [];
    const owners: Array<[number, number]> = [];
    results.forEach((result, index) => {
      const target = firstTarget(result);
      if (target === null) return;
      owners.push([
        Math.floor(index / PROPERTY_BROWSE_NAMES.length),
        index % PROPERTY_BROWSE_NAMES.length,
      ]);
      targets.push(target);
    });

    const values: DataValue[] = [];
    for (const chunk of chunked(targets)) {
      values.push(
        ...(await session.read(
          chunk.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value }))
        ))
      );
    }

    const found = new Map<string, unknown[]>(
      nodeIds.map((nodeId) => [nodeId, new Array(PROPERTY_BROWSE_NAMES.length).fill(null)])
    );
    owners.forEach(([nodeIndex, propertyIndex], position) => {
      const dataValue = values[position];
      if (!dataValue || dataValue.statusCode !== StatusCodes.Good) return;
      found.get(nodeIds[nodeIndex])![propertyIndex] = dataValue.value?.value ?? null;
    });

    return new Map(
      [...found].map(([nodeId, properties]) => [nodeId, assemble(properties)] as const)
    );
  }
}
