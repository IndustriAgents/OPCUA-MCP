/** Alarm result/framing semantics; the port performs one native action per invocation. */
import { AdapterFailure, ContractRefusal, describeError, message } from "../errors.js";
import { canonicalNodeId } from "../node-ids.js";
import { checkAlarmScope } from "../policy-check.js";
/** A condition's SourceNode and Severity, or why each could not be read. */
export interface ConditionScope {
  source: string | null;
  severity: number | null;
  sourceProblem: string | null;
  severityProblem: string | null;
}
/** The operator's alarm scope, resolved; null members are not configured. */
export interface AlarmScope {
  sources: string[] | null;
  maxSeverity: number | null;
}
export interface AlarmPort {
  list(nodeId: string, timeout: number): Promise<unknown[]>;
  remember(records: unknown[]): void;
  conditionFor(eventId: string): string | null;
  /** Reads the condition's SourceNode and Severity. A connection error propagates. */
  scopeOf(conditionId: string): Promise<ConditionScope>;
  action(
    conditionId: string,
    eventId: string,
    action: string,
    comment: string,
    duration: number | null
  ): Promise<{ status: string; good: boolean }>;
}
export async function listAlarms(
  port: AlarmPort,
  nodeId: string,
  timeout: number
): Promise<unknown[]> {
  let records: unknown[];
  try {
    records = await port.list(nodeId, timeout);
  } catch (error) {
    throw new AdapterFailure(
      "alarms",
      message("alarmsFailed", { node_id: nodeId, reason: describeError(error) }),
      error
    );
  }
  port.remember(records);
  return records;
}
/** Refuse to act on a condition outside the operator's alarm scope (spec §6.4).
 *
 * Reads the condition's SourceNode and Severity only when the scope sets either.
 * A value that cannot be read refuses: an alarm whose scope cannot be checked is
 * not acknowledged on the policy's behalf.
 */
async function requireAlarmScope(
  port: AlarmPort,
  conditionId: string,
  scope: AlarmScope | null
): Promise<void> {
  if (scope === null) return;
  const read = await port.scopeOf(conditionId);
  const error =
    scope.sources !== null && read.sourceProblem !== null
      ? `SourceNode: ${read.sourceProblem}`
      : scope.maxSeverity !== null && read.severityProblem !== null
        ? `Severity: ${read.severityProblem}`
        : null;
  const refusal = checkAlarmScope({
    condition_id: conditionId,
    source: read.source,
    severity: read.severity,
    error,
    sources: scope.sources,
    max_severity: scope.maxSeverity,
  });
  if (refusal !== null) throw new ContractRefusal(refusal);
}
export async function actOnAlarm(
  port: AlarmPort,
  eventId: string,
  action: string,
  comment: string,
  duration: number | null,
  conditionId: string | undefined,
  acknowledgement: boolean,
  scope: AlarmScope | null = null
) {
  if (action === "shelveFor" && duration === null)
    throw new ContractRefusal(message("shelveForNeedsDuration"));
  if (action !== "shelveFor" && duration !== null)
    throw new ContractRefusal(message("shelveDurationNotAllowed", { action }));
  const condition = conditionId || port.conditionFor(eventId);
  if (!condition) throw new ContractRefusal(message("unknownEventId", { event_id: eventId }));
  const failed = (reason: string) =>
    acknowledgement
      ? message("acknowledgeFailed", { condition_id: condition, reason })
      : message("alarmActionFailed", { action, condition_id: condition, reason });
  // Before anything is sent: a condition outside the scope, or one whose scope
  // cannot be read, is refused with nothing changed.
  await requireAlarmScope(port, condition, scope);
  let status: { status: string; good: boolean };
  try {
    status = await port.action(condition, eventId, action, comment, duration);
  } catch (error) {
    throw new AdapterFailure("alarm-action", failed(describeError(error)), error);
  }
  if (!status.good) throw new AdapterFailure("alarm-action", failed(status.status), undefined);
  const record = {
    event_id: eventId,
    condition_id: canonicalNodeId(condition),
    status: status.status,
  };
  return acknowledgement ? record : { ...record, action, status: status.status };
}
