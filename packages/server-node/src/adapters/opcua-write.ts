import { dispatchControl } from "../infrastructure/control-context.js";
/** node-opcua write adapter. Prepared native values belong to this one call. */
import {
  AttributeIds,
  DataType,
  DataValue,
  Variant,
  VariantArrayType,
  type ClientSession,
} from "node-opcua-client";
import type { CurrentValue, PlannedWrite, WritePort, WriteRequest } from "../application/write.js";
import { checkMaxChange as checkNormalizedChange } from "../application/write.js";
import { AdapterFailure, ContractRefusal, describeError } from "../errors.js";
import { chunked } from "../limits.js";
import type { NodeFacts, NodeFactsCache } from "../node-facts.js";
import type { NodeMetadata } from "../node-metadata.js";
import type { RequirementReading } from "../policy-check.js";
import { asNumber } from "../policy.js";
import { variantToJson } from "../records.js";
import { isGood } from "../status.js";
import { convertForVariant } from "../variant-codec.js";
import { readRequirements } from "./opcua-interlocks.js";

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
    private readonly metadata: NodeMetadata,
    private readonly nodeFacts: NodeFactsCache | null = null,
    private readonly readChunk = Infinity
  ) {}

  /** What each node says about itself, from the session's cache. Never throws:
   *  facts that cannot be read are null, and the server decides. */
  async facts(nodeIds: string[]): Promise<Map<string, NodeFacts | null>> {
    if (this.nodeFacts === null) return new Map();
    return await this.nodeFacts.forNodes(this.session, nodeIds);
  }

  async requirements(nodeIds: string[]): Promise<RequirementReading[]> {
    return await readRequirements(this.session, nodeIds, this.readChunk);
  }

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
      return await this.metadata.forControl(this.session, nodeIds);
    } catch (error) {
      throw new AdapterFailure("write-metadata", describeError(error), error);
    }
  }

  async prepare(
    write: PlannedWrite,
    index: number
  ): Promise<{ status: string; error: string } | null> {
    try {
      let dataType: DataType;
      let arrayType = VariantArrayType.Scalar;
      let dimensions: number[] | null = null;
      if (write.data_type !== null) {
        const named = DataType[write.data_type as keyof typeof DataType];
        if (typeof named !== "number") throw new Error(`Unknown data_type "${write.data_type}"`);
        dataType = named;
        if (write.array ?? Array.isArray(write.value)) arrayType = VariantArrayType.Array;
      } else {
        const value = this.readings.get(index);
        if (!value || !isGood(value.statusCode) || !value.value)
          return {
            status: value?.statusCode?.name ?? "BadUnexpectedError",
            error:
              "could not read the node's data type to convert the value; give data_type to write without reading it first",
          };
        dataType = value.value.dataType;
        // The node's ValueRank, where it said one, decides the shape; the
        // current value's only where it did not.
        if (write.array === null) {
          arrayType = value.value.arrayType;
          dimensions = value.value.dimensions;
        } else if (write.array) {
          arrayType = VariantArrayType.Array;
        }
      }
      const value = convertForVariant(write.value, dataType, arrayType);
      this.writes.push({
        nodeId: write.node_id,
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
      return (await dispatchControl(() => this.session.write(this.writes))).map(
        (status) => status.name
      );
    } catch (error) {
      throw new AdapterFailure("write", describeError(error), error);
    }
  }
}
