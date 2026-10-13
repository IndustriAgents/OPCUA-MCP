/** Native status read and connection recovery behind the diagnostics port. */
import type { ClientSession } from "node-opcua-client";
import type { OpcuaConnection } from "../connection.js";
import type { ServerIdentityRecord } from "../policy.js";
import type { DiagnosticsPort, ServerStatusRecord } from "../application/diagnostics.js";
import { readServerStatus } from "../diagnostics.js";
import { AdapterFailure, describeError } from "../errors.js";
export class NodeOpcuaDiagnosticsPort<C> implements DiagnosticsPort<C> {
  constructor(
    private readonly connection: OpcuaConnection,
    private readonly session: () => ClientSession,
    private readonly report: () => C
  ) {}
  snapshot() {
    return {
      endpoint: this.connection.endpointUrl,
      connecting: this.connection.connecting,
      lastError: this.connection.lastErrorMessage,
    };
  }
  capabilities() {
    return this.report();
  }
  async read(security: string, identity: ServerIdentityRecord): Promise<ServerStatusRecord> {
    try {
      return await this.connection.withRetry(async () => {
        try {
          return await readServerStatus(
            this.session(),
            this.connection.endpointUrl,
            security,
            identity
          );
        } catch (error) {
          throw new AdapterFailure("diagnostics", describeError(error), error);
        }
      });
    } catch (error) {
      if (error instanceof AdapterFailure) throw error;
      throw new AdapterFailure("diagnostics", describeError(error), error);
    }
  }
}
