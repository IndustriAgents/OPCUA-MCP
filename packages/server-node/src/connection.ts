// Owns the OPC UA client/session lifecycle and the runtime capability probes.
//
// Capability probes are best-effort by design: an optional capability must
// never break tools/list, so a transient outage still leaves the core tools
// advertised.
import { OPCUAClient, ClientSession, StatusCodes, AggregateFunction } from "node-opcua";

import { setDefaultAutoSelectFamily } from "net";

import { SERVER_URL } from "./config.js";
import { CONTRACT } from "./contract.js";
import {
  clientSecurityOptions,
  describeSecurity,
  isInsecure,
  securityConfig,
  userIdentity,
} from "./security.js";

// Happy Eyeballs: try IPv4 and IPv6 rather than only the first address DNS
// returns. Node 20+ does this by default, Node 18 does not — so on Node 18 an
// endpoint like the default `opc.tcp://localhost:4840` resolves to ::1 and fails
// outright against an OPC UA server listening on IPv4, instead of falling back
// to 127.0.0.1. Guarded because the API landed in Node 18.13.
if (typeof setDefaultAutoSelectFamily === "function") {
  setDefaultAutoSelectFamily(true);
}

export class OpcuaConnection {
  private opcuaClient: OPCUAClient | null = null;
  session: ClientSession | null = null;

  async connect(): Promise<void> {
    try {
      if (this.opcuaClient && this.session) {
        return; // Already connected
      }

      const security = securityConfig();
      if (isInsecure(security)) {
        console.error(
          "WARNING: connecting with no OPC UA security — traffic is unencrypted and " +
            "unauthenticated. Set OPCUA_SECURITY_POLICY (and OPCUA_USERNAME) for anything " +
            "beyond local development."
        );
      }

      this.opcuaClient = OPCUAClient.create({
        applicationName: "OPC UA MCP Client",
        connectionStrategy: {
          initialDelay: 1000,
          maxRetry: 1,
        },
        ...clientSecurityOptions(security),
        endpoint_must_exist: false,
      });

      await this.opcuaClient.connect(SERVER_URL);
      console.error(`Connected to OPC UA server (${describeSecurity(security)})`);

      this.session = await this.opcuaClient.createSession(userIdentity(security));
      console.error("OPC UA session created");
    } catch (error) {
      console.error("Failed to connect to OPC UA server:", error);
      throw error;
    }
  }

  async disconnect(): Promise<void> {
    try {
      if (this.session) {
        await this.session.close();
        this.session = null;
        console.error("OPC UA session closed");
      }

      if (this.opcuaClient) {
        await this.opcuaClient.disconnect();
        this.opcuaClient = null;
        console.error("Disconnected from OPC UA server");
      }
    } catch (error) {
      console.error("Error during disconnect:", error);
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
