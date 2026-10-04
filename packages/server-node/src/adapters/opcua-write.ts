/** node-opcua write adapter. Prepared native values belong to this one call. */
import {
  AttributeIds,
  DataType,
  DataValue,
  Variant,
  VariantArrayType,
  type ClientSession,
} from "node-opcua-client";
import type { CurrentValue, WritePort, WriteRequest } from "../application/write.js";
import { checkMaxChange as checkNormalizedChange } from "../application/write.js";
import { AdapterFailure, ContractRefusal, describeError } from "../errors.js";
import { chunked } from "../limits.js";
import type { NodeMetadata } from "../node-metadata.js";
import { asNumber } from "../policy.js";
import { variantToJson } from "../records.js";
import { isGood } from "../status.js";
import { convertForVariant } from "../variant-codec.js";

export function currentValue(value: DataValue | undefined): CurrentValue | undefined {
  if (!value) return undefined;
  const good = isGood(value.statusCode);
  return {
    good,
    status: value.statusCode.name,
    number: good ? asNumber(variantToJson(value.value)) : null,
  };
}
export function checkMaxChange(
  nodeId: string,
  value: unknown,
  limit: number,
  dataValue: DataValue | undefined
): void {
  checkNormalizedChange(nodeId, value, limit, currentValue(dataValue));
}

export class NodeOpcuaWritePort implements WritePort {
  private readonly readings = new Map<number, DataValue | undefined>();
  private readonly writes: Array<{ nodeId: string; attributeId: AttributeIds; value: DataValue }> =
    [];
  constructor(
    private readonly session: ClientSession,
    private readonly metadata: NodeMetadata
  ) {}

  async current(
    nodes: WriteRequest[],
    indices: number[],
    chunk: number
  ): Promise<Map<number, CurrentValue>> {
    try {
      const values: DataValue[] = [];
      for (const part of chunked(indices, chunk))
        values.push(
          ...(await this.session.read(
            part.map((index) => ({ nodeId: nodes[index].node_id, attributeId: AttributeIds.Value }))
          ))
        );
      const normalized = new Map<number, CurrentValue>();
      indices.forEach((index, position) => {
        const value = values[position];
        this.readings.set(index, value);
        const record = currentValue(value);
        if (record) normalized.set(index, record);
      });
      return normalized;
    } catch (error) {
      throw new AdapterFailure("write-read", describeError(error), error);
    }
  }

  async engineering(nodeIds: string[]) {
    try {
      return await this.metadata.forNodes(this.session, nodeIds);
    } catch (error) {
      throw new AdapterFailure("write-metadata", describeError(error), error);
    }
  }

  async prepare(
    node: WriteRequest,
    index: number
  ): Promise<{ status: string; error: string } | null> {
    try {
      let dataType: DataType;
      let arrayType = VariantArrayType.Scalar;
      let dimensions: number[] | null = null;
      if (node?.data_type) {
        const named = DataType[node.data_type as keyof typeof DataType];
        if (typeof named !== "number") throw new Error(`Unknown data_type "${node.data_type}"`);
        dataType = named;
        if (Array.isArray(node.value)) arrayType = VariantArrayType.Array;
      } else {
        const value = this.readings.get(index);
        if (!value || !isGood(value.statusCode) || !value.value)
          return {
            status: value?.statusCode?.name ?? "BadUnexpectedError",
            error:
              "could not read the node's data type to convert the value; give data_type to write without reading it first",
          };
        dataType = value.value.dataType;
        arrayType = value.value.arrayType;
        dimensions = value.value.dimensions;
      }
      const value = convertForVariant(node.value, dataType, arrayType);
      this.writes.push({
        nodeId: node.node_id,
        attributeId: AttributeIds.Value,
        value: new DataValue({ value: new Variant({ dataType, arrayType, dimensions, value }) }),
      });
      return null;
    } catch (error) {
      if (error instanceof ContractRefusal) throw error;
      return { status: "BadTypeMismatch", error: describeError(error) };
    }
  }

  async send(): Promise<string[]> {
    try {
      return (await this.session.write(this.writes)).map((status) => status.name);
    } catch (error) {
      throw new AdapterFailure("write", describeError(error), error);
    }
  }
}
