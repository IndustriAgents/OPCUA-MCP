/** Operator interlocks (control.preconditions), checked right before a write or call is sent. */
import { ContractRefusal, message } from "../errors.js";
import { checkPreconditions, type RequirementReading } from "../policy-check.js";
import type { Precondition } from "../policy.js";

/** One thing about to be changed, as the caller named it, and what guards it. */
export interface Interlock {
  /** A node id, or `object|method` for a call. */
  target: string;
  preconditions: Precondition[];
}

/** What the interlock check needs from the policy resolved on this session. */
export interface InterlockPolicy {
  unresolvedPrecondition(): string | null;
  resolveEntry(entry: string): string | null;
}

/** Bound to one session by the adapter. */
export interface InterlockPort {
  /** Each node's current Value, in order. A connection error propagates; any
   *  other failure is that node's reading with the failure as its status. */
  requirements(nodeIds: string[]): Promise<RequirementReading[]>;
}

/** Refuse unless every operator precondition guarding these targets holds now.
 *
 * First, all control while an interlock's target does not resolve (spec §12): it
 * is a refusal of the call rather than of a node, because the interlock that
 * does not resolve may be guarding any of them. Then the required nodes are read
 * in one Read, right before sending, so what is checked is the plant as it is
 * and not as it was when the session began. A requirement node that does not
 * resolve, or does not read Good, does not hold: an interlock nobody can confirm
 * is an interlock that is not met.
 */
export async function requireInterlocks(
  port: InterlockPort,
  policy: InterlockPolicy,
  targets: Interlock[]
): Promise<void> {
  const entry = policy.unresolvedPrecondition();
  if (entry !== null) throw new ContractRefusal(message("preconditionUnresolved", { entry }));

  const guarded = targets.filter((target) => target.preconditions.length > 0);
  if (guarded.length === 0) return;
  const nodes = [
    ...new Set(
      guarded.flatMap((target) =>
        target.preconditions.flatMap((precondition) =>
          precondition.require.map((requirement) => requirement.node)
        )
      )
    ),
  ];
  const current: Record<string, RequirementReading> = {};
  const toRead: Array<{ node: string; nodeId: string }> = [];
  for (const node of nodes) {
    const nodeId = policy.resolveEntry(node);
    if (nodeId === null) current[node] = null;
    else toRead.push({ node, nodeId });
  }
  if (toRead.length > 0) {
    const readings = await port.requirements(toRead.map(({ nodeId }) => nodeId));
    toRead.forEach(({ node }, index) => {
      current[node] = readings[index] ?? null;
    });
  }
  for (const { target, preconditions } of guarded) {
    for (const precondition of preconditions) {
      const refusal = checkPreconditions({ target, requirements: precondition.require, current });
      if (refusal !== null) throw new ContractRefusal(refusal);
    }
  }
}
