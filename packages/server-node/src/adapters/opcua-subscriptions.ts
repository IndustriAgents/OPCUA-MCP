/** node-opcua subscription adapter keeps sessions and monitored items behind the port. */
import type { ClientSession } from "node-opcua-client";
import type {
  SubscriptionPort,
  SubscribeOptions,
  SubscriptionFilter,
  SubscriptionRecord,
} from "../application/subscriptions.js";
import { AdapterFailure, describeError } from "../errors.js";
import type { NodeMetadata } from "../node-metadata.js";
import { SubscriptionManager } from "../subscriptions.js";
export class NodeOpcuaSubscriptionPort implements SubscriptionPort {
  constructor(
    private readonly session: () => ClientSession,
    private readonly manager: SubscriptionManager,
    private readonly metadata: NodeMetadata
  ) {}
  list(): SubscriptionRecord[] {
    return this.manager.list();
  }
  async ranges(nodeIds: string[]): Promise<Map<string, boolean>> {
    try {
      return new Map(
        [...(await this.metadata.forNodes(this.session(), nodeIds))].map(([id, info]) => [
          id,
          !!info?.eu_range,
        ])
      );
    } catch (error) {
      throw new AdapterFailure("subscription-metadata", describeError(error), error);
    }
  }
  async subscribe(
    nodeId: string,
    options: SubscribeOptions,
    filter: SubscriptionFilter
  ): Promise<SubscriptionRecord> {
    try {
      return await this.manager.subscribe(this.session(), nodeId, options, filter);
    } catch (error) {
      throw new AdapterFailure("subscribe", describeError(error), error);
    }
  }
  async unsubscribe(id: string): Promise<SubscriptionRecord> {
    try {
      return await this.manager.unsubscribe(id);
    } catch (error) {
      throw new AdapterFailure("unsubscribe", describeError(error), error);
    }
  }
}
