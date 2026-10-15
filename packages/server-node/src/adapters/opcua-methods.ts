import { dispatchControl } from "../infrastructure/control-context.js";
/** Native method metadata, variants and service result codecs. */
import {
  AttributeIds,
  DataType,
  Variant,
  VariantArrayType,
  makeBrowsePath,
  type Argument,
  type ClientSession,
  type CallMethodResult,
} from "node-opcua-client";
import { type MethodPort, type MethodType, type MethodArgument } from "../application/methods.js";
import { supertypeOf } from "../browse.js";
import { AdapterFailure, ContractRefusal, describeError } from "../errors.js";
import { builtInType, guessVariant } from "../method-arguments.js";
import type { NodeFacts, NodeFactsCache } from "../node-facts.js";
import { canonicalNodeId } from "../node-ids.js";
import type { RequirementReading } from "../policy-check.js";
import { variantToJson } from "../records.js";
import { isGood } from "../status.js";
import { convertForVariant } from "../variant-codec.js";
import { readRequirements } from "./opcua-interlocks.js";
export class NodeOpcuaMethodPort implements MethodPort {
  /** The method's InputArguments, once read for this call; see `inputArguments`. */
  private definition: { methodNodeId: string; inputs: Argument[] | null } | null = null;
  constructor(
    private readonly session: ClientSession,
    private readonly nodeFacts: NodeFactsCache | null = null,
    private readonly readChunk = Infinity
  ) {}
  async facts(nodeIds: string[]): Promise<Map<string, NodeFacts | null>> {
    if (this.nodeFacts === null) return new Map();
    return await this.nodeFacts.forNodes(this.session, nodeIds);
  }
  async onObject(objectId: string, methodId: string): Promise<boolean | null> {
    if (this.nodeFacts === null) return null;
    return await this.nodeFacts.onObject(this.session, objectId, methodId);
  }
  async declaredArguments(methodNodeId: string): Promise<number | null> {
    return (await this.inputArguments(methodNodeId))?.length ?? null;
  }
  async requirements(nodeIds: string[]): Promise<RequirementReading[]> {
    return await readRequirements(this.session, nodeIds, this.readChunk);
  }
  async inputTypes(methodNodeId: string): Promise<MethodType[]> {
    try {
      const types = await this.nativeInputTypes(methodNodeId);
      return types.map((t) => ({
        dataType: DataType[t.dataType],
        isArray: t.arrayType === VariantArrayType.Array,
      }));
    } catch (error) {
      if (error instanceof ContractRefusal) throw error;
      throw new AdapterFailure("method", describeError(error), error);
    }
  }
  async call(
    objectNodeId: string,
    methodNodeId: string,
    args: MethodArgument[]
  ): Promise<{ status: string; outputs: unknown[] }> {
    try {
      const inputArguments = args.map((arg, index) => {
        if (arg.dataType === null) return guessVariant(arg.value, index);
        const dataType = DataType[arg.dataType as keyof typeof DataType] as DataType;
        const arrayType = arg.isArray ? VariantArrayType.Array : VariantArrayType.Scalar;
        return new Variant({
          dataType,
          arrayType,
          value: convertForVariant(arg.value, dataType, arrayType),
        });
      });
      const result: CallMethodResult = await dispatchControl(() =>
        this.session.call({
          objectId: objectNodeId,
          methodId: methodNodeId,
          inputArguments,
        })
      );
      if (!isGood(result.statusCode))
        throw new Error(`Method call failed with status: ${result.statusCode.name}`);
      return {
        status: result.statusCode.name,
        outputs: (result.outputArguments ?? []).map(variantToJson),
      };
    } catch (error) {
      if (error instanceof ContractRefusal) throw error;
      throw new AdapterFailure("method", describeError(error), error);
    }
  }

  /** The method's InputArguments, or null when it publishes none.
   *
   * Null and empty are different answers, which node-opcua's
   * `getArgumentDefinition` does not tell apart: a method that publishes an
   * empty list takes no arguments, and one that publishes nothing says nothing
   * about how many it takes — so only the first refuses a call that passes some.
   * One translate and one read, as python-opcua's `get_child` makes, once per
   * call; anything that goes wrong is "publishes none", the old fallback.
   */
  private async inputArguments(methodNodeId: string): Promise<Argument[] | null> {
    if (this.definition?.methodNodeId === methodNodeId) return this.definition.inputs;
    let inputs: Argument[] | null = null;
    try {
      const [result] = await this.session.translateBrowsePath([
        makeBrowsePath(methodNodeId, "/0:InputArguments"),
      ]);
      const target = result && isGood(result.statusCode) ? result.targets?.[0] : undefined;
      if (target) {
        const dataValue = await this.session.read({
          nodeId: target.targetId.toString(),
          attributeId: AttributeIds.Value,
        });
        if (isGood(dataValue.statusCode)) {
          const value: unknown = dataValue.value?.value;
          if (value === null || value === undefined) inputs = [];
          else if (Array.isArray(value)) inputs = value as Argument[];
        }
      }
    } catch {
      inputs = null;
    }
    this.definition = { methodNodeId, inputs };
    return inputs;
  }

  /** The declared type of each input argument, or [] when the method publishes none.
   *
   * A declared DataType that is not itself built in (`Duration`, `UtcTime`, an
   * enumeration) is resolved to the built-in type it is encoded as, and one that
   * resolves to none throws: that is a method whose argument cannot be encoded,
   * not one that declares nothing, and guessing would send it anyway.
   */
  private async nativeInputTypes(
    methodNodeId: string
  ): Promise<Array<{ dataType: DataType; arrayType: VariantArrayType }>> {
    const declared = [];
    for (const argument of (await this.inputArguments(methodNodeId)) ?? []) {
      declared.push({
        dataType: await builtInType(canonicalNodeId(argument.dataType.toString()), (dataType) =>
          supertypeOf(this.session, dataType)
        ),
        arrayType: argument.valueRank >= 1 ? VariantArrayType.Array : VariantArrayType.Scalar,
      });
    }
    return declared;
  }
}
