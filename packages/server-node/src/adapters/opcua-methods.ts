/** Native method metadata, variants and service result codecs. */
import {
  DataType,
  Variant,
  VariantArrayType,
  BrowseDirection,
  type ClientSession,
  type CallMethodResult,
} from "node-opcua-client";
import { type MethodPort, type MethodType, type MethodArgument } from "../application/methods.js";
import { AdapterFailure, ContractRefusal, describeError } from "../errors.js";
import { builtInType, guessVariant } from "../method-arguments.js";
import { canonicalNodeId } from "../node-ids.js";
import { variantToJson } from "../records.js";
import { isGood } from "../status.js";
import { convertForVariant } from "../variant-codec.js";
const HAS_SUBTYPE = 45;
export class NodeOpcuaMethodPort implements MethodPort {
  constructor(private readonly session: ClientSession) {}
  async inputTypes(methodNodeId: string): Promise<MethodType[]> {
    try {
      const types = await this.nativeInputTypes(this.session, methodNodeId);
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
      const result: CallMethodResult = await this.session.call({
        objectId: objectNodeId,
        methodId: methodNodeId,
        inputArguments,
      });
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
  /** The declared type of each input argument, or [] when the method publishes none.
   *
   * A declared DataType that is not itself built in (`Duration`, `UtcTime`, an
   * enumeration) is resolved to the built-in type it is encoded as, and one that
   * resolves to none throws: that is a method whose argument cannot be encoded,
   * not one that declares nothing, and guessing would send it anyway.
   */
  private async nativeInputTypes(
    session: ClientSession,
    methodNodeId: string
  ): Promise<Array<{ dataType: DataType; arrayType: VariantArrayType }>> {
    let definition;
    try {
      definition = await session.getArgumentDefinition(methodNodeId);
    } catch {
      // Not every method publishes InputArguments, and a method with no
      // arguments has nothing to publish. Fall back rather than refuse.
      return [];
    }

    const supertypeOf = async (dataType: string): Promise<string | null> => {
      // Every inverse reference, filtered here rather than by the server:
      // python-opcua's server answers a browse filtered to HasSubtype with
      // nothing at all, and the Python runtime does the same for that reason.
      const result = await session.browse({
        nodeId: dataType,
        browseDirection: BrowseDirection.Inverse,
        resultMask: 63,
      });
      if (!isGood(result.statusCode)) return null;
      const parent = (result.references ?? []).find(
        (reference) =>
          reference.referenceTypeId.namespace === 0 &&
          reference.referenceTypeId.value === HAS_SUBTYPE
      );
      return parent ? canonicalNodeId(parent.nodeId.toString()) : null;
    };

    const declared = [];
    for (const argument of definition.inputArguments ?? []) {
      declared.push({
        dataType: await builtInType(canonicalNodeId(argument.dataType.toString()), supertypeOf),
        arrayType: argument.valueRank >= 1 ? VariantArrayType.Array : VariantArrayType.Scalar,
      });
    }
    return declared;
  }
}
