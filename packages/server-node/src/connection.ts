// Owns the OPC UA client/session lifecycle and the runtime capability probes.
//
// Capability probes are best-effort by design: an optional capability must
// never break tools/list, so a transient outage still leaves the core tools
// advertised.
// `node-opcua-client`, not the umbrella `node-opcua`: this is an OPC UA *client*,
// and the umbrella package's entry point drags in the server implementation
// alongside it. That half is not merely dead weight in the downloadable bundles
// — `node-opcua-server` and the address-space test helpers both read files
// relative to their own `__dirname` at *import* time to find their package.json,
// which no longer exists once bundled, so the `.mcpb` died on connect. Importing
// the client package keeps the compiler honest about what this server may use.
import { OPCUAClient, ClientSession, StatusCodes, AggregateFunction } from "node-opcua-client";

import { setDefaultAutoSelectFamily } from "net";

import { SERVER_URL } from "./config.js";
import { CONTRACT } from "./contract.js";
import {
  clientSecurityOptions,
  describeSecurity,
  securityConfig,
  securityWarnings,
  userIdentity,
} from "./security.js";

// Happy Eyeballs: try IPv4 and IPv6 rather than only the first address DNS
// returns. Every Node this package now supports defaults to this, so the call is
// belt and braces — but an explicit `true` also survives a deployment that turns
// it off with `--no-network-family-autoselection`, under which the default
// `opc.tcp://localhost:4840` resolves to ::1 and fails outright against an OPC UA
// server listening on IPv4 rather than falling back to 127.0.0.1.
setDefaultAutoSelectFamily(true);

export class OpcuaConnection {
  private opcuaClient: OPCUAClient | null = null;
  private connectPromise: Promise<void> | null = null;
  session: ClientSession | null = null;

  async connect(): Promise<void> {
    if (this.opcuaClient && this.session) return;
    if (!this.connectPromise) {
      this.connectPromise = this.open().finally(() => {
        this.connectPromise = null;
      });
    }
    return this.connectPromise;
  }

  private async open(): Promise<void> {
    let client: OPCUAClient | null = null;
    try {
      const security = securityConfig();
      for (const warning of securityWarnings(security)) {
        console.error(`WARNING: ${warning}`);
      }

      client = OPCUAClient.create({
        applicationName: "OPC UA MCP Client",
        connectionStrategy: {
          initialDelay: 1000,
          maxRetry: 1,
        },
        ...clientSecurityOptions(security),
        endpoint_must_exist: false,
      });

      await client.connect(SERVER_URL);
      console.error(`Connected to OPC UA server (${describeSecurity(security)})`);

      const session = await client.createSession(userIdentity(security));
      this.opcuaClient = client;
      this.session = session;
      console.error("OPC UA session created");
    } catch (error) {
      if (client) {
        try {
          await client.disconnect();
        } catch {
          // Preserve the original connection error.
        }
      }
      this.opcuaClient = null;
      this.session = null;
      console.error("Failed to connect to OPC UA server:", error);
      throw error;
    }
  }

  async disconnect(): Promise<void> {
    if (this.connectPromise) {
      try {
        await this.connectPromise;
      } catch {
        // A failed open already cleaned up its partial client.
      }
    }

    const session = this.session;
    const client = this.opcuaClient;
    this.session = null;
    this.opcuaClient = null;

    if (session) {
      try {
        await session.close();
        console.error("OPC UA session closed");
      } catch (error) {
        console.error("Error closing OPC UA session:", error);
      }
    }

    if (client) {
      try {
        await client.disconnect();
        console.error("Disconnected from OPC UA server");
      } catch (error) {
        console.error("Error disconnecting OPC UA client:", error);
      }
    }
  }

  async ensureConnection(): Promise<void> {
    if (!this.opcuaClient || !this.session) {
      await this.connect();
    }
  }

  async accessHistoryDataCapability(): Promise<boolean> {
    // Best-effort: never let an optional capability probe break tools/list. A
    // transient OPC UA outage should still leave the core tools advertised.
    try {
      await this.ensureConnection();
      const dataValue = await this.session!.readVariableValue(CONTRACT.capabilities.history.nodeId);
      return dataValue.statusCode === StatusCodes.Good && dataValue.value?.value === true;
    } catch (error) {
      console.error("accessHistoryDataCapability probe failed:", error);
      return false;
    }
  }

  async serverCapabilitiesAggregateFunctions(): Promise<string[]> {
    // Best-effort: any failure (incl. a connection error) yields no aggregate
    // functions rather than breaking tools/list.
    let aggregateFunctions: string[] = [];
    try {
      await this.ensureConnection();
      const browseResult = await this.session!.browse({
        nodeId: CONTRACT.capabilities.aggregate.nodeId,
        browseDirection: 0, // Forward
        resultMask: 63, // All information (including BrowseName)
      });
      if (browseResult.statusCode === StatusCodes.Good && browseResult.references) {
        for (const reference of browseResult.references) {
          // Map the string BrowseName to the AggregateFunction
          if (reference.browseName.name) {
            const name = reference.browseName.name.toString();
            if (name in AggregateFunction) {
              aggregateFunctions.push(name);
            }
          }
        }
      }
    } catch (error) {
      console.error("Error during serverCapabilitiesAggregateFunctions:", error);
    }
    return aggregateFunctions;
  }
}
