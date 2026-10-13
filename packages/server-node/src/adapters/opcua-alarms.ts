/** node-opcua alarm adapter. Session selection stays lazy until a service is required. */
import type { ClientSession } from "node-opcua-client";
import type { AlarmPort } from "../application/alarms.js";
import { AdapterFailure, describeError } from "../errors.js";
import { EventSubscriptions, type EventRecord, alarmAction, listActiveAlarms } from "../events.js";
import { isGood } from "../status.js";
export class NodeOpcuaAlarmPort implements AlarmPort {
  constructor(
    private readonly session: () => ClientSession,
    private readonly events: EventSubscriptions
  ) {}
  remember(records: unknown[]): void {
    this.events.remember(records as EventRecord[]);
  }
  conditionFor(eventId: string): string | null {
    return this.events.conditionFor(eventId) ?? null;
  }
  async list(nodeId: string, timeout: number): Promise<unknown[]> {
    try {
      return await listActiveAlarms(this.session(), nodeId, timeout);
    } catch (error) {
      throw new AdapterFailure("alarms", describeError(error), error);
    }
  }
  async action(
    conditionId: string,
    eventId: string,
    action: string,
    comment: string,
    duration: number | null
  ): Promise<{ status: string; good: boolean }> {
    try {
      const status = await alarmAction(
        this.session(),
        conditionId,
        eventId,
        action,
        comment,
        duration
      );
      return { status: status.name, good: isGood(status) };
    } catch (error) {
      throw new AdapterFailure("alarm-action", describeError(error), error);
    }
  }
}
