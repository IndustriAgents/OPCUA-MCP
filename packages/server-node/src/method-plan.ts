// Whether a method call can work, decided from the two nodes' own attributes.
//
// `method_plan.py` is the Python half; `tests/fixtures/method-plan.json` drives
// both. A call to the wrong node, to a method that is switched off, or with the
// wrong number of arguments used to go out and come back as a status code —
// BadMethodInvalid, BadNotExecutable, BadArgumentsMissing — which said that the
// plant refused it and nothing about why. Every one of them is something the
// address space already says, so it is refused here, before anything is sent,
// with a sentence that names the fix.
//
// As with writes, a fact that could not be read skips its check: the server
// still decides, and nothing here can make a call possible that was not.
import { message } from "./errors.js";
import { factsGood, type NodeFacts } from "./node-facts.js";

/** What `planCall` is told about one call. */
export interface CallPlanInput {
  object_node_id: string;
  method_node_id: string;
  object_facts: NodeFacts | null;
  method_facts: NodeFacts | null;
  /** How many InputArguments the method publishes, or null when it publishes none. */
  declared_arguments: number | null;
  given_arguments: number;
  /** Whether the method is the object's or its type's; null when that could not
   *  be told. */
  on_object: boolean | null;
}

/** The node classes a method may be called on (Part 4 §5.11.2). */
const CALLABLE_ON = new Set(["Object", "ObjectType"]);

/** null to call, or the refusal for the first thing that would stop it (spec §3). */
export function planCall(input: CallPlanInput): string | null {
  const { object_node_id: objectId, method_node_id: methodId } = input;
  const method = input.method_facts;
  const object = input.object_facts;
  if (factsGood(method) && method.node_class !== "Method") {
    return message("methodNotAMethod", {
      method_node_id: methodId,
      node_class: method.node_class ?? "unknown",
    });
  }
  if (factsGood(object) && !CALLABLE_ON.has(object.node_class ?? "")) {
    return message("methodObjectNotAnObject", {
      object_node_id: objectId,
      node_class: object.node_class ?? "unknown",
    });
  }
  if (method?.executable === false) {
    return message("methodNotExecutable", { method_node_id: methodId });
  }
  if (method?.user_executable === false) {
    return message("methodNotExecutableForUser", { method_node_id: methodId });
  }
  if (input.on_object === false) {
    return message("methodNotOnObject", { method_node_id: methodId, object_node_id: objectId });
  }
  if (input.declared_arguments !== null && input.declared_arguments !== input.given_arguments) {
    return message("methodArgumentCount", {
      method_node_id: methodId,
      expected: input.declared_arguments,
      count: input.given_arguments,
    });
  }
  return null;
}
