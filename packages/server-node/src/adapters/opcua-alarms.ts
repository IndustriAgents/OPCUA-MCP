/** node-opcua alarm adapter. Session selection stays lazy until a service is required. */
import { AttributeIds, makeBrowsePath, type ClientSession } from "node-opcua-client";
import type { AlarmPort, ConditionScope } from "../application/alarms.js";
import { isConnectionError } from "../connection.js";
import { AdapterFailure, describeError } from "../errors.js";
import { canonicalNodeId } from "../node-ids.js";
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

  /** The condition's SourceNode and Severity: one translate and one read.
   *
   * A value that cannot be read is that half's problem, which the scope check
   * refuses on. A connection error propagates, as the action would have met it.
   */
  async scopeOf(conditionId: string): Promise<ConditionScope> {
    const scope: ConditionScope = {
      source: null,
      severity: null,
      sourceProblem: null,
      severityProblem: null,
    };
    try {
      const session = this.session();
      const results = await session.translateBrowsePath([
        makeBrowsePath(conditionId, "/0:SourceNode"),
        makeBrowsePath(conditionId, "/0:Severity"),
      ]);
      const targets = results.map((result) =>
        isGood(result.statusCode) ? (result.targets?.[0]?.targetId.toString() ?? null) : null
      );
      const found = targets.filter((target): target is string => target !== null);
      const values =
        found.length > 0
          ? await session.read(found.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value })))
          : [];
      let position = 0;
      results.forEach((result, index) => {
        const source = index === 0;
        let problem: string | null = null;
        let value: unknown = undefined;
        if (targets[index] === null) {
          problem = isGood(result.statusCode) ? "BadNoMatch" : result.statusCode.name;
        } else {
          const dataValue = values[position++];
          if (!dataValue || !isGood(dataValue.statusCode))
            problem = dataValue?.statusCode?.name ?? "BadUnexpectedError";
          else value = dataValue.value?.value;
        }
        if (source) {
          scope.sourceProblem = problem;
          scope.source = value ? canonicalNodeId(String(value.toString())) : null;
        } else {
          scope.severityProblem = problem;
          scope.severity = typeof value === "number" ? value : null;
        }
      });
    } catch (error) {
      if (isConnectionError(error))
        throw new AdapterFailure("alarm-scope", describeError(error), error);
      scope.sourceProblem = describeError(error);
      scope.severityProblem = describeError(error);
    }
    return scope;
  }
}
