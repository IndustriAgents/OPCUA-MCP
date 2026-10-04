/** Native browse service and codecs, bound to one selected session. */
import {
  AttributeIds,
  DataType,
  NodeClass,
  BrowseDirection,
  type ClientSession,
  type DataValue,
  type Variant,
} from "node-opcua-client";
import { type BrowsePort, type NodeRefRecord } from "../application/browse.js";
import { browseAllReferences, typeDefinitionOf } from "../browse.js";
import { CONTRACT } from "../contract.js";
import { AdapterFailure, describeError } from "../errors.js";
import { chunked } from "../limits.js";
import { canonicalNodeId } from "../node-ids.js";
import { isGood } from "../status.js";
import { variantToJson } from "../records.js";
import { type ServerOperationLimits, browseChunk, readChunk } from "../operation-limits.js";
function dataTypeName(variant: Variant | null | undefined): string | null {
  const dataType = variant?.dataType;
  if (dataType === undefined || dataType === null || dataType === DataType.Null) return null;
  return DataType[dataType] ?? null;
}

/** The OPC UA name behind a DataType *attribute*, which is a NodeId, not an enum. */
function dataTypeNameFromNodeId(value: unknown): string | null {
  const node = value as { value?: unknown; namespace?: number } | null;
  const identifier = node?.value;
  if (node?.namespace !== 0 || typeof identifier !== "number") return null;
  return DataType[identifier] ?? null;
}

async function readValues(
  session: ClientSession,
  items: Array<{ nodeId: string; attributeId: AttributeIds }>,
  chunk: number
): Promise<DataValue[]> {
  const values: DataValue[] = [];
  for (const part of chunked(items, chunk)) {
    values.push(...(await session.read(part)));
  }
  return values;
}

export class NodeOpcuaBrowsePort implements BrowsePort {
  constructor(
    private readonly session: ClientSession,
    private readonly limits: () => Promise<ServerOperationLimits>
  ) {}
  async children(nodeId: string) {
    try {
      const refs = await browseAllReferences(this.session, nodeId);
      return refs.map((ref) => ({
        nodeId: canonicalNodeId(ref.nodeId.toString()),
        namespaceIndex: ref.browseName.namespaceIndex,
        name: ref.browseName.name,
        nodeClass: NodeClass[ref.nodeClass] ?? "Unspecified",
      }));
    } catch (error) {
      throw new AdapterFailure("browse", describeError(error), error);
    }
  }
  async enrich(records: NodeRefRecord[], includeValues: boolean) {
    const limits = await this.limits();
    await this.fillTypeDefinitions(this.session, records, limits);
    if (includeValues) await this.fillVariableDetail(this.session, records, limits);
  }
  async describe(nodeId: string, parentNodeId: string) {
    try {
      return await this.describeNative(nodeId, parentNodeId);
    } catch (error) {
      throw new AdapterFailure("browse", describeError(error), error);
    }
  }
  /** The record for one node read directly, rather than off a browse reference. */
  private async describeNative(nodeId: string, parentNodeId: string): Promise<NodeRefRecord> {
    const [browseName, nodeClass] = await this.session.read([
      { nodeId, attributeId: AttributeIds.BrowseName },
      { nodeId, attributeId: AttributeIds.NodeClass },
    ]);
    if (!isGood(browseName.statusCode)) {
      throw new Error(`Browse failed with status: ${browseName.statusCode.name}`);
    }
    const name = browseName.value?.value;
    return {
      node_id: canonicalNodeId(nodeId),
      browse_name: name ? `${name.namespaceIndex}:${name.name}` : "",
      node_class: NodeClass[nodeClass.value?.value as number] ?? "Unspecified",
      parent_node_id: canonicalNodeId(parentNodeId),
      data_type: null,
      value: null,
      description: null,
      type_definition: null,
    };
  }

  /** Fill in `type_definition` for `records`, in one batched browse.
   *
   * `HasTypeDefinition` is non-hierarchical, so the traversal's own browse —
   * forward hierarchical references only, deliberately, or every node would
   * answer with its parent and its type instead of its children — never sees
   * it. It takes a second browse, and that is why this is one request for the
   * whole result rather than one per node: a 500-node walk would otherwise cost
   * 500 extra round trips to say what one already could.
   *
   * Best-effort, like the variable detail: a server that refuses this leaves the
   * field null rather than failing a browse that succeeded.
   */
  private async fillTypeDefinitions(
    session: ClientSession,
    records: NodeRefRecord[],
    serverLimits: ServerOperationLimits
  ): Promise<void> {
    if (records.length === 0) return;
    const traversal = CONTRACT.traversal;
    const descriptions = records.map((record) => ({
      nodeId: record.node_id,
      browseDirection: BrowseDirection.Forward,
      referenceTypeId: traversal.hasTypeDefinitionNodeId,
      // No subtypes: HasTypeDefinition has none, and asking for them would let
      // an unrelated reference through on a server that has invented one.
      includeSubtypes: false,
      nodeClassMask: 0,
      resultMask: 63,
    }));

    // Chunked for the same reason the property reads are: MaxNodesPerBrowse is
    // an operational limit a conformant server may enforce, and the default
    // walk already returns up to 500 nodes. A server that states a lower one
    // gets smaller chunks.
    const size = browseChunk(serverLimits, traversal.maxTypeDefinitionsPerRequest);
    for (let start = 0; start < descriptions.length; start += size) {
      let results;
      try {
        results = await session.browse(descriptions.slice(start, start + size));
      } catch {
        return;
      }
      results.forEach((result, index) => {
        records[start + index].type_definition = typeDefinitionOf(
          isGood(result.statusCode),
          (result.references ?? []).map((reference) => reference.browseName.name ?? "")
        );
      });
    }
  }

  /** Fill in value, data type and description for the Variables among `records`.
   *
   * One `read` for everything rather than three per node: a 500-node inventory
   * is otherwise 1500 round trips, which is the difference between a tool that
   * answers and one that times out on real equipment.
   */
  private async fillVariableDetail(
    session: ClientSession,
    records: NodeRefRecord[],
    serverLimits: ServerOperationLimits
  ): Promise<void> {
    const variables = records.filter((record) => record.node_class === "Variable");
    if (variables.length === 0) return;

    const reads = variables.flatMap((record) => [
      { nodeId: record.node_id, attributeId: AttributeIds.Value },
      { nodeId: record.node_id, attributeId: AttributeIds.DataType },
      { nodeId: record.node_id, attributeId: AttributeIds.Description },
    ]);
    let values;
    try {
      // Three attributes per node, so a 500-node walk is a 1500-item read — the
      // largest single request this server made, and one it used to send whole.
      values = await readValues(session, reads, readChunk(serverLimits));
    } catch {
      // Best-effort enrichment: the nodes were found, and reporting them
      // without their values beats failing a browse that succeeded.
      return;
    }

    variables.forEach((record, index) => {
      const [value, dataType, description] = values.slice(index * 3, index * 3 + 3);
      if (value && isGood(value.statusCode)) {
        record.value = variantToJson(value.value);
        record.data_type = dataTypeName(value.value);
      }
      if (record.data_type === null && dataType && isGood(dataType.statusCode)) {
        record.data_type = dataTypeNameFromNodeId(dataType.value?.value);
      }
      const text = description?.value?.value?.text;
      record.description = typeof text === "string" && text.length > 0 ? text : null;
    });
  }
}
