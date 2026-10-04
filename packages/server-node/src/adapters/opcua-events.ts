/** node-opcua event adapter normalizes library failures before returning records. */
import type { ClientSession } from "node-opcua-client";
import type { EventPort, EventDrain, EventPage } from "../application/events.js";
import { AdapterFailure, describeError } from "../errors.js";
import { EventSubscriptions, readEventHistory } from "../events.js";
export class NodeOpcuaEventPort implements EventPort {
  constructor(
    private readonly session: ClientSession,
    private readonly events: EventSubscriptions
  ) {}
  async subscribe(nodeId: string, severity: number, size: number): Promise<boolean> {
    try {
      return (await this.events.subscribe(this.session, nodeId, severity, size)).replaced;
    } catch (error) {
      throw new AdapterFailure("event-subscribe", describeError(error), error);
    }
  }
  async drain(nodeId: string, limit: number): Promise<EventDrain | null> {
    return this.events.drain(this.session, nodeId, limit);
  }
  async history(
    nodeId: string,
    start: Date,
    end: Date,
    wanted: number,
    severity: number
  ): Promise<EventPage> {
    try {
      return await readEventHistory(this.session, nodeId, start, end, wanted, severity);
    } catch (error) {
      throw new AdapterFailure("event-history", describeError(error), error);
    }
  }
}
