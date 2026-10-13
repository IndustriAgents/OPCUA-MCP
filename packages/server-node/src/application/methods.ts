/** Method orchestration over JSON-facing metadata and one native call. */
import { canonicalNodeId } from "../node-ids.js";
import { ContractRefusal, describeError, message } from "../errors.js";
import { planCall } from "../method-plan.js";
import type { NodeFacts } from "../node-facts.js";
import type { Precondition } from "../policy.js";
import { requireInterlocks, type InterlockPolicy, type InterlockPort } from "./interlocks.js";
export interface MethodType {
  dataType: string;
  isArray: boolean;
}
export interface MethodArgument {
  value: unknown;
  dataType: string | null;
  isArray: boolean;
}
export interface MethodPort extends InterlockPort {
  /** What each node says about itself; null where nothing could be read. Never throws. */
  facts(nodeIds: string[]): Promise<Map<string, NodeFacts | null>>;
  /** Whether the method is the object's or its type's; null when that could not
   *  be told. Both ids canonical. Never throws. */
  onObject(objectId: string, methodId: string): Promise<boolean | null>;
  /** How many InputArguments the method publishes, or null when it publishes
   *  none — which is not the same answer as an empty list. Never throws. */
  declaredArguments(methodNodeId: string): Promise<number | null>;
  inputTypes(methodNodeId: string): Promise<MethodType[]>;
  call(
    objectNodeId: string,
    methodNodeId: string,
    args: MethodArgument[]
  ): Promise<{ status: string; outputs: unknown[] }>;
}
/** The operator's interlocks, as the call path asks about them. */
export interface MethodInterlocks extends InterlockPolicy {
  resolveNodeId(nodeId: string): string | null;
  preconditionsForCall(object: string, method: string): Precondition[];
}

/** Refuse a call the address space already rules out (method-plan.ts).
 *
 * A node that is not a Method, a method that is switched off or not the
 * object's, the wrong number of arguments: each was a status code from the plant
 * after the call went out, and each is refused here with nothing sent.
 */
async function requireCallable(
  port: MethodPort,
  objectNodeId: string,
  methodNodeId: string,
  given: number,
  resolve: (nodeId: string) => string | null
): Promise<void> {
  const facts = await port.facts([objectNodeId, methodNodeId]);
  const objectId = resolve(objectNodeId);
  const methodId = resolve(methodNodeId);
  const refusal = planCall({
    object_node_id: objectNodeId,
    method_node_id: methodNodeId,
    object_facts: facts.get(objectNodeId) ?? null,
    method_facts: facts.get(methodNodeId) ?? null,
    declared_arguments: await port.declaredArguments(methodNodeId),
    given_arguments: given,
    on_object:
      objectId !== null && methodId !== null ? await port.onObject(objectId, methodId) : null,
  });
  if (refusal !== null) throw new ContractRefusal(refusal);
}

export async function callMethod(
  port: MethodPort,
  objectNodeId: string,
  methodNodeId: string,
  args: unknown[] | null = [],
  interlocks: MethodInterlocks | null = null
) {
  const given = args ?? [];
  try {
    await requireCallable(
      port,
      objectNodeId,
      methodNodeId,
      given.length,
      interlocks === null ? canonicalNodeId : (nodeId) => interlocks.resolveNodeId(nodeId)
    );
    if (interlocks !== null) {
      await requireInterlocks(port, interlocks, [
        {
          target: `${objectNodeId}|${methodNodeId}`,
          preconditions: interlocks.preconditionsForCall(objectNodeId, methodNodeId),
        },
      ]);
    }
    const declared = await port.inputTypes(methodNodeId);
    const result = await port.call(
      objectNodeId,
      methodNodeId,
      given.map((value, index) => ({
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
