/** Alarm result/framing semantics; the port performs one native action per invocation. */
import { AdapterFailure, ContractRefusal, describeError, message } from "../errors.js";
import { canonicalNodeId } from "../node-ids.js";
export interface AlarmPort {
  list(nodeId: string, timeout: number): Promise<unknown[]>;
  remember(records: unknown[]): void;
  conditionFor(eventId: string): string | null;
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
export async function actOnAlarm(
  port: AlarmPort,
  eventId: string,
  action: string,
  comment: string,
  duration: number | null,
  conditionId: string | undefined,
  acknowledgement: boolean
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
