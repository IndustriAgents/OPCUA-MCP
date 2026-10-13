/** node-opcua read adapter. Native data and exceptions end at this boundary. */
import {
  AttributeIds,
  DataType,
  type ClientSession,
  type DataValue,
  type Variant,
} from "node-opcua-client";
import { type ReadPort } from "../application/read.js";
import { AdapterFailure, describeError, message } from "../errors.js";
import { type AnalogInfo, NodeMetadata } from "../node-metadata.js";
import { canonicalNodeId } from "../node-ids.js";
import { isGood } from "../status.js";
import { toIsoUtc, variantToJson } from "../records.js";

function dataTypeName(variant: Variant | null | undefined): string | null {
  const type = variant?.dataType;
  return type === undefined || type === null || type === DataType.Null
    ? null
    : (DataType[type] ?? null);
}

/** One node's reading (resultShapes.nodeValues). */
export interface NodeValueRecord {
  node_id: string;
  value: unknown;
  data_type: string | null;
  status: string;
  source_timestamp: string | null;
  server_timestamp: string | null;
  engineering: AnalogInfo | null;
}

/** One node's reading as a canonical record (resultShapes.nodeValues).
 *
 * The value goes through the *shared* codec, so a Boolean is `true` on both
 * runtimes rather than `true` here and `True` there, and an Int64 is a number
 * or a numeric string rather than node-opcua's `[high, low]` pair. Reading used
 * to stringify natively and so diverged by construction — the one thing
 * `value-encoding.json` exists to prevent, just outside its reach.
 */
export function toNodeValueRecord(
  nodeId: string,
  dataValue: DataValue | undefined,
  engineering: AnalogInfo | null = null
): NodeValueRecord {
  const good = dataValue !== undefined && isGood(dataValue.statusCode);
  return {
    node_id: canonicalNodeId(nodeId),
    value: good ? variantToJson(dataValue?.value) : null,
    data_type: good ? dataTypeName(dataValue?.value) : null,
    // An absent status code means Good in OPC UA, so name it rather than null.
    status: dataValue?.statusCode?.name ?? "Good",
    source_timestamp: toIsoUtc(dataValue?.sourceTimestamp),
    server_timestamp: toIsoUtc(dataValue?.serverTimestamp),
    // What the plant says this number means. null for most nodes, because only
    // an AnalogItemType publishes it — but on the ones that do it is the
    // difference between "51.75" and "51.75 °C, normal range 0 to 150".
    engineering,
  };
}

export class NodeOpcuaReadPort implements ReadPort {
  constructor(
    private readonly session: ClientSession,
    private readonly metadata: NodeMetadata
  ) {}

  async values(nodeIds: string[]): Promise<NodeValueRecord[]> {
    try {
      const values = await this.session.read(
        nodeIds.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value }))
      );
      return nodeIds.map((nodeId, index) => toNodeValueRecord(nodeId, values[index]));
    } catch (error) {
      throw new AdapterFailure(
        "read",
        message("readFailed", { reason: describeError(error) }),
        error
      );
    }
  }

  async engineering(nodeIds: string[]): Promise<Map<string, AnalogInfo | null>> {
    try {
      return await this.metadata.forNodes(this.session, nodeIds);
    } catch (error) {
      throw new AdapterFailure(
        "read",
        message("readFailed", { reason: describeError(error) }),
        error
      );
    }
  }
}
