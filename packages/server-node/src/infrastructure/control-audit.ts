/** Durable control-audit adapter; records contain targets and session facts only. */
import { randomBytes } from "crypto";
import { CONTRACT, type ToolSpec } from "../contract.js";
import { AuditSink, AuditWriteError, buildRecord, operatorId } from "../audit.js";
import { ToolPolicy, controlGate, valuesAt } from "../policy.js";
import { ContractRefusal, message } from "../errors.js";
export interface AuditConnection {
  endpointUrl: string;
  sessionId: string | null;
  sessionGeneration: number | null;
}
export function describeTargets(tool: ToolSpec, args: Record<string, unknown>): string {
  const targets = auditTargets(tool, args);
  const parts = Object.entries(targets).map(
    ([key, value]) => `${key}=${Array.isArray(value) ? value.join(", ") : String(value)}`
  );
  return parts.length > 0 ? parts.join("; ") : "unknown";
}

export function auditTargets(
  tool: ToolSpec,
  args: Record<string, unknown>
): Record<string, unknown> {
  const guard = tool.guard;
  if (!guard) return {};
  const record: Record<string, unknown> = {};

  const nodeIds = (guard.nodeIdPaths ?? []).flatMap((path) => valuesAt(args, path));
  if (nodeIds.length > 0) record.node_ids = nodeIds;

  const methods = (guard.methodPaths ?? []).map(({ objectPath, methodPath }) => ({
    object_node_id: valuesAt(args, objectPath)[0] ?? null,
    method_node_id: valuesAt(args, methodPath)[0] ?? null,
  }));
  if (methods.length > 0) Object.assign(record, methods[0]);

  for (const path of guard.auditPaths ?? []) {
    // Only what is present: an absent optional argument is not a target, and
    // recording it as null would make every acknowledgement look half-specified.
    const [value] = valuesAt(args, path);
    if (value !== undefined) record[path] = value;
  }
  return record;
}

export function auditDecision(
  sink: AuditSink,
  policy: ToolPolicy,
  connection: AuditConnection,
  name: string,
  args: Record<string, unknown>,
  decision: "allowed" | "denied" | "failed" | "completed",
  callId: string,
  attempt: number,
  reason?: string
): void {
  const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
  if (!tool || !["control", "alarm-action"].includes(tool.accessClass)) return;
  sink.write(
    buildRecord({
      timestamp: new Date().toISOString(),
      call_id: callId,
      attempt,
      endpoint: connection.endpointUrl,
      session: connection.sessionId,
      session_generation: connection.sessionGeneration,
      // null when OPCUA_OPERATOR_ID is unset, which is honest: this server has no
      // notion of who is calling, and a name nothing verified would be worse
      // than none.
      operator_label: operatorId(),
      ...sink.identity(),
      profile: policy.config.profile,
      // What let control through, or kept it out: `secured` for a verified
      // server, or which lab override was in force. An override that shows up
      // only in a startup line nobody kept is an override nobody can audit.
      control: controlGate(policy.config),
      ...(policy.config.serverIdentity?.authenticationMethod === "trust-store"
        ? { server_authentication_method: "trust-store" as const }
        : {}),
      tool: name,
      decision,
      targets: auditTargets(tool, args),
      reason: reason ?? null,
    })
  );
}

export function auditPermission(
  sink: AuditSink,
  policy: ToolPolicy,
  connection: AuditConnection,
  name: string,
  args: Record<string, unknown>,
  callId: string,
  attempt: number
): void {
  try {
    auditDecision(sink, policy, connection, name, args, "allowed", callId, attempt);
  } catch (error) {
    if (!(error instanceof AuditWriteError)) throw error;
    console.error(`AUDIT FAILURE: refusing ${name} (call ${callId}): ${error.message}`);
    throw new ContractRefusal(message("auditUnavailable", { tool: name, reason: error.message }));
  }
}

export function auditAfter(...args: Parameters<typeof auditDecision>): void {
  try {
    auditDecision(...args);
  } catch (error) {
    if (!(error instanceof AuditWriteError)) throw error;
    const [, , , name, , decision, callId] = args;
    console.error(
      `AUDIT FAILURE: the ${decision} record for ${name} (call ${callId}) was not written: ${error.message}`
    );
  }
}

export function newCallId(): string {
  return randomBytes(8).toString("hex");
}
