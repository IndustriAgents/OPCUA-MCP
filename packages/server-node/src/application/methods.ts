/** Method orchestration over JSON-facing metadata and one native call. */
import { canonicalNodeId } from "../node-ids.js";
import { ContractRefusal, describeError, message } from "../errors.js";
export interface MethodType {
  dataType: string;
  isArray: boolean;
}
export interface MethodArgument {
  value: unknown;
  dataType: string | null;
  isArray: boolean;
}
export interface MethodPort {
  inputTypes(methodNodeId: string): Promise<MethodType[]>;
  call(
    objectNodeId: string,
    methodNodeId: string,
    args: MethodArgument[]
  ): Promise<{ status: string; outputs: unknown[] }>;
}
export async function callMethod(
  port: MethodPort,
  objectNodeId: string,
  methodNodeId: string,
  args: unknown[] | null = []
) {
  try {
    const declared = await port.inputTypes(methodNodeId);
    const result = await port.call(
      objectNodeId,
      methodNodeId,
      (args ?? []).map((value, index) => ({
        value,
        dataType: declared[index]?.dataType ?? null,
        isArray: declared[index]?.isArray ?? false,
      }))
    );
    return {
      object_node_id: canonicalNodeId(objectNodeId),
      method_node_id: canonicalNodeId(methodNodeId),
      status: result.status,
      outputs: result.outputs,
    };
  } catch (error) {
    if (error instanceof ContractRefusal) throw error;
    throw new ContractRefusal(
      message("methodFailed", {
        method_node_id: methodNodeId,
        object_node_id: objectNodeId,
        reason: describeError(error),
      }),
      { cause: error }
    );
  }
}
