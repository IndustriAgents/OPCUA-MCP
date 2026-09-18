// The OPC UA tool implementations.
//
// Adding a tool touches this file and contract/tools.json, and nothing else:
// `listTools` is generated from the contract, `callTool` dispatches by name.
import {
  AttributeIds,
  DataType,
  Variant,
  DataValue,
  StatusCodes,
  CallMethodResult,
  ReferenceDescription,
  HistoryData,
  AggregateFunction,
  ClientSession,
} from "node-opcua-client";
import { Resource, Tool } from "@modelcontextprotocol/sdk/types.js";

import { browseAllReferences } from "./browse.js";
import { OpcuaConnection, notConnectedMessage } from "./connection.js";
import { CONTRACT } from "./contract.js";
import { ServerStatusRecord, disconnectedStatus, readServerStatus } from "./diagnostics.js";
import { toDate } from "./dates.js";
import {
  DEFAULT_NOTIFIER,
  EVENT_DEFAULTS,
  EventRecord,
  EventSubscriptions,
  acknowledgeAlarm,
  droppedEventsMessage,
  listActiveAlarms,
} from "./events.js";
import { toHistoryRecords } from "./records.js";
import { describeSecurity, securityConfig } from "./security.js";
import { SubscriptionManager, SubscriptionRecord } from "./subscriptions.js";
import { ToolPolicy, toolPolicy } from "./policy.js";
import { convertForVariant } from "./variant-codec.js";

/** A history/aggregate response: one text block per canonical record.
 *
 * The framing is part of the contract (resultShapes.historyRecords), not an
 * implementation detail: FastMCP splits the Python server's returned list into
 * one block per element, so the Node server does the same rather than emitting a
 * single array — the two servers' responses are then read the same way.
 */
function historyResult(dataValues: DataValue[] | null | undefined) {
  return recordBlocks(toHistoryRecords(dataValues));
}

/** The same framing for the subscription family (resultShapes.subscriptionRecords). */
function subscriptionResult(records: SubscriptionRecord[]) {
  return recordBlocks(records);
}

/** And for the event family (resultShapes.eventRecords). */
function eventResult(records: EventRecord[]) {
  return recordBlocks(records);
}

/** The diagnostics report (resultShapes.serverStatus).
 *
 * One object rather than a list, so one text block and a `result` that is the
 * object itself — the Python server's `get_server_status` frames it identically.
 */
function statusResult(status: ServerStatusRecord) {
  return {
    content: [{ type: "text", text: JSON.stringify(status, null, 2) }],
    structuredContent: { result: status },
  };
}

function recordBlocks(records: unknown[]) {
  return {
    content: records.map((record) => ({
      type: "text",
      text: JSON.stringify(record, null, 2),
    })),
    structuredContent: { result: records },
  };
}

function outputSchema(resultShape: string | undefined): Tool["outputSchema"] {
  if (!resultShape) return undefined;
  return {
    type: "object",
    properties: { result: CONTRACT.resultShapes[resultShape] },
    required: ["result"],
    additionalProperties: false,
  } as Tool["outputSchema"];
}

/** Whether a tool may be run a second time when the first attempt found a dead session.
 *
 * Keyed on the contract's own `idempotentHint`, so the question is answered once,
 * where the tool is declared, rather than in a list here that could disagree with
 * what tools/list tells the model. A dead session almost certainly means the
 * request never reached the server — but "almost certainly" is not a licence to
 * fire `call_opcua_method` twice at a machine, so the non-idempotent tools report
 * the failure and leave the retry to a human.
 */
function retryIsSafe(name: string): boolean {
  const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
  return tool?.annotations.idempotentHint === true;
}

function auditTargets(name: string, args: Record<string, unknown>): Record<string, unknown> {
  if (name === "write_opcua_node") return { node_ids: [args.node_id] };
  if (name === "write_multiple_opcua_nodes") {
    const items = Array.isArray(args.nodes_to_write) ? args.nodes_to_write : [];
    return {
      node_ids: items.map((item) => (item as Record<string, unknown>).node_id),
    };
  }
  if (name === "call_opcua_method") {
    return { object_node_id: args.object_node_id, method_node_id: args.method_node_id };
  }
  if (name === "acknowledge_alarm") {
    return { condition_id: args.condition_id, event_id: args.event_id };
  }
  return {};
}

function auditDecision(
  policy: ToolPolicy,
  name: string,
  args: Record<string, unknown>,
  decision: "allowed" | "denied",
  reason?: string
): void {
  const tool = CONTRACT.tools.find((candidate) => candidate.name === name);
  if (!tool || !["control", "alarm-action"].includes(tool.accessClass)) return;
  console.error(
    JSON.stringify({
      event: "opcua_mcp_policy",
      timestamp: new Date().toISOString(),
      profile: policy.config.profile,
      tool: name,
      decision,
      ...auditTargets(name, args),
      ...(reason ? { reason } : {}),
    })
  );
}

export class OpcuaTools {
  private aggregateFunctions: string[] = [];
  private readonly subs = new SubscriptionManager();
  private readonly events = new EventSubscriptions();

  constructor(
    private readonly conn: OpcuaConnection,
    private readonly policy: ToolPolicy = toolPolicy()
  ) {
    // A rebuilt connection is a new session, and an OPC UA subscription belongs
    // to the session that created it. Without this, a server restart would leave
    // every `subscribe_opcua_node` handle the agent holds silently dead.
    this.conn.onSessionReplaced = (session) => this.subs.reattach(session);
  }

  /** Tear down every OPC UA subscription this server created.
   *
   * Called before the session is closed, on every shutdown path. Closing the
   * session alone would leave the OPC UA server publishing to nobody until the
   * subscription's lifetime expired. Event subscriptions are subscriptions too,
   * and cost the server the same until they expire.
   */
  async shutdown(): Promise<void> {
    await this.subs.closeAll();
    await this.events.closeAll();
  }

  // Delegations that keep the tool bodies below identical to their previous
  // form as methods of the old monolithic server class.
  private get session(): ClientSession | null {
    return this.conn.session;
  }

  private ensureConnection(): Promise<void> {
    return this.conn.ensureConnection();
  }

  private serverCapabilitiesAggregateFunctions(): Promise<string[]> {
    return this.conn.serverCapabilitiesAggregateFunctions();
  }

  private accessHistoryDataCapability(): Promise<boolean> {
    return this.conn.accessHistoryDataCapability();
  }

  /** The advertised tool list: the contract, gated by runtime capabilities. */
  async listTools(): Promise<Tool[]> {
    // Build the advertised tools from the shared contract, gated by the
    // server's runtime capabilities (history / aggregate).
    //
    // One connection attempt for both probes, not one each: against a server
    // that is down, each probe would otherwise sit through the whole configured
    // backoff on its own and double what a tools/list costs. A failure here is
    // not fatal — the core tools are advertised regardless, and the optional
    // ones reappear on the next tools/list once the server is back.
    await this.conn.ensureConnection().catch(() => undefined);
    const probeable = this.conn.connected;
    const historyOk = probeable && (await this.accessHistoryDataCapability());
    this.aggregateFunctions = probeable ? await this.serverCapabilitiesAggregateFunctions() : [];
    const aggregateOk = this.aggregateFunctions.length > 0;

    const tools = this.policy
      .visibleTools(CONTRACT.tools)
      .filter(
        (t) =>
          t.capability === null ||
          (t.capability === "history" && historyOk) ||
          (t.capability === "aggregate" && aggregateOk)
      )
      .map((t) => {
        // Preserve the dynamic aggregate_function help text (lists the
        // aggregate functions the server actually advertises).
        if (t.name === "read_aggregate_opcua_node") {
          const inputSchema = JSON.parse(JSON.stringify(t.inputSchema));
          inputSchema.properties.aggregate_function.description =
            t.inputSchema.properties.aggregate_function.description +
            ", one of: " +
            [...this.aggregateFunctions].join(", ");
          return {
            name: t.name,
            description: t.description,
            inputSchema,
            annotations: t.annotations,
            outputSchema: outputSchema(t.resultShape),
          };
        }
        return {
          name: t.name,
          description: t.description,
          inputSchema: t.inputSchema,
          annotations: t.annotations,
          outputSchema: outputSchema(t.resultShape),
        };
      }) satisfies Tool[];

    return tools;
  }

  /** The advertised resource list: taken straight from the contract. */
  listResources(): Resource[] {
    return CONTRACT.resources.map((r) => ({
      uri: r.uri,
      name: r.name,
      description: r.description,
      mimeType: r.mimeType,
    }));
  }

  /** Serve a resources/read request.
   *
   * Deliberately does not touch the OPC UA server: this reports what the
   * subscriptions have already delivered, so it stays readable — and honest —
   * even while the connection is down.
   */
  readResource(uri: string) {
    const resource = CONTRACT.resources.find((r) => r.uri === uri);
    if (!resource) {
      throw new Error(`Unknown resource: ${uri}`);
    }
    return {
      contents: [
        {
          uri: resource.uri,
          mimeType: resource.mimeType,
          text: JSON.stringify({ [resource.body.recordsKey]: this.subs.list() }, null, 2),
        },
      ],
    };
  }

  /** Serve a tools/call request: authorize it, then run it on a live session. */
  async callTool(request: { params: { name: string; arguments?: Record<string, unknown> } }) {
    const { name, arguments: args } = request.params;

    try {
      // This is the security boundary. Filtering tools/list improves the model's
      // choices, but clients cache catalogs and may call a previously visible
      // tool directly, so authorize again before touching the OPC UA network.
      try {
        this.policy.authorize(name, args ?? {});
        auditDecision(this.policy, name, args ?? {}, "allowed");
      } catch (error) {
        auditDecision(
          this.policy,
          name,
          args ?? {},
          "denied",
          error instanceof Error ? error.message : String(error)
        );
        throw error;
      }
      // The one tool that must answer while the connection is down: it exists to
      // say so. Everything below needs a session first.
      if (name === "get_server_status") {
        return statusResult(await this.getServerStatus());
      }

      // Connecting is attempted before dispatching, so that a server that is
      // simply not there is reported as that rather than as a puzzling failure
      // from whichever tool happened to be called first.
      try {
        await this.ensureConnection();
      } catch (error) {
        throw new Error(
          notConnectedMessage(
            this.conn.endpointUrl,
            error instanceof Error ? error.message : String(error)
          )
        );
      }

      return await this.conn.withRetry(() => this.dispatch(name, args ?? {}), retryIsSafe(name));
    } catch (error) {
      return {
        content: [
          {
            type: "text",
            text: `Error: ${error instanceof Error ? error.message : String(error)}`,
          },
        ],
        isError: true,
      };
    }
  }

  /** Run one tool. The caller has already authorized it and ensured a session. */
  private async dispatch(name: string, args: Record<string, unknown>) {
    switch (name) {
      case "read_opcua_node":
        return await this.readOpcuaNode(args?.node_id as string);

      case "read_history_opcua_node":
        return await this.readHistoryOpcuaNode(
          args?.node_id as string,
          args?.start_time as string | undefined,
          args?.end_time as string | undefined,
          (args?.num_values as number) || 0
        );

      case "read_aggregate_opcua_node":
        return await this.readAggregateOpcuaNode(
          args?.node_id as string,
          args?.start_time as string,
          args?.end_time as string | undefined,
          args?.aggregate_function as string,
          (args?.processing_interval as number) || 0
        );

      case "write_opcua_node":
        return await this.writeOpcuaNode(args?.node_id as string, args?.value);

      case "browse_opcua_node_children":
        return await this.browseOpcuaNodeChildren(args?.node_id as string);

      case "read_multiple_opcua_nodes":
        return await this.readMultipleOpcuaNodes(args?.node_ids as string[]);

      case "write_multiple_opcua_nodes":
        return await this.writeMultipleOpcuaNodes(
          args?.nodes_to_write as Array<{ node_id: string; value: unknown }>
        );

      case "call_opcua_method":
        return await this.callOpcuaMethod(
          args?.object_node_id as string,
          args?.method_node_id as string,
          args?.arguments as string[]
        );

      case "get_all_variables":
        return await this.getAllVariables(
          (args?.root_node_id as string | undefined) ?? "ns=0;i=85",
          (args?.max_depth as number | undefined) ?? 8,
          (args?.max_nodes as number | undefined) ?? 500,
          (args?.include_values as boolean | undefined) ?? true
        );

      case "subscribe_opcua_node":
        return subscriptionResult([
          await this.subs.subscribe(this.requireSession(), args?.node_id as string, {
            publishingInterval: args?.publishing_interval as number | undefined,
            samplingInterval: args?.sampling_interval as number | undefined,
            bufferSize: args?.buffer_size as number | undefined,
          }),
        ]);

      case "list_subscriptions":
        return subscriptionResult(this.subs.list());

      case "unsubscribe_opcua_node":
        return await this.unsubscribeOpcuaNode(args?.subscription_id as string);

      case "subscribe_events":
        return await this.subscribeEvents(
          (args?.node_id as string) || DEFAULT_NOTIFIER,
          (args?.severity_min as number) ?? EVENT_DEFAULTS.severityMin,
          (args?.buffer_size as number) || EVENT_DEFAULTS.bufferSize
        );

      case "read_events":
        return this.readEvents(
          (args?.node_id as string) || DEFAULT_NOTIFIER,
          (args?.limit as number) || EVENT_DEFAULTS.readLimit
        );

      case "list_active_alarms":
        return await this.listActiveAlarms(
          (args?.node_id as string) || DEFAULT_NOTIFIER,
          (args?.timeout_seconds as number) ?? EVENT_DEFAULTS.refreshTimeoutSeconds
        );

      case "acknowledge_alarm":
        return await this.acknowledgeAlarm(
          args?.event_id as string,
          (args?.comment as string) ?? "",
          args?.condition_id as string | undefined
        );

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  }

  /** The `get_server_status` report: connection state, then what the server says.
   *
   * Connecting is attempted rather than assumed, so asking for the status is
   * also the cheapest way to bring a dropped connection back. A failure to
   * connect is the answer, not an error — "not connected, and here is why" is
   * exactly what the caller asked for.
   */
  private async getServerStatus(): Promise<ServerStatusRecord> {
    const endpoint = this.conn.endpointUrl;
    const security = describeSecurity(securityConfig());
    try {
      // Through the same retry as every other read, so that asking for the
      // status also re-establishes a session that has silently died — which is
      // exactly the moment someone asks.
      return await this.conn.withRetry(() =>
        readServerStatus(this.requireSession(), endpoint, security)
      );
    } catch (error) {
      return disconnectedStatus(
        endpoint,
        security,
        error instanceof Error ? error.message : String(error)
      );
    }
  }

  private requireSession(): ClientSession {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }
    return this.session;
  }

  private async unsubscribeOpcuaNode(subscriptionId: string) {
    const record = await this.subs.unsubscribe(subscriptionId);
    return {
      content: [
        {
          type: "text",
          text:
            `Unsubscribed ${record.subscription_id} from node ${record.node_id} ` +
            `after ${record.change_count} value changes`,
        },
      ],
    };
  }

  private async readOpcuaNode(nodeId: string) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      const dataValue = await this.session.readVariableValue(nodeId);

      if (dataValue.statusCode !== StatusCodes.Good) {
        throw new Error(`Read failed with status: ${dataValue.statusCode.toString()}`);
      }

      const value = dataValue.value?.value;
      return {
        content: [
          {
            type: "text",
            text: `Node ${nodeId} value: ${value}`,
          },
        ],
      };
    } catch (error) {
      throw new Error(
        `Failed to read node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async readHistoryOpcuaNode(
    nodeId: string,
    start: string | undefined,
    end: string | undefined,
    numValuesPerNode: number
  ) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      const historyValues = await this.session.readHistoryValue(
        [nodeId],
        toDate(start) as any,
        toDate(end) as any,
        {
          numValuesPerNode,
        }
      );
      if (historyValues.length !== 1) {
        throw new Error(`Read history failed`);
      }
      if (historyValues[0].statusCode !== StatusCodes.Good) {
        throw new Error(
          `Read history failed with status: ${historyValues[0].statusCode.toString()}`
        );
      }
      const dataValues = (historyValues[0].historyData as HistoryData).dataValues;
      return historyResult(dataValues);
    } catch (error) {
      throw new Error(
        `Failed to read node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async readAggregateOpcuaNode(
    nodeId: string,
    start: string,
    end: string | undefined,
    aggregate_fn: string,
    processing_interval: number
  ) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    // Don't depend on a prior tools/list having populated the cache: a client may
    // call this tool directly after connecting. Recompute on demand if empty.
    if (this.aggregateFunctions.length === 0) {
      this.aggregateFunctions = await this.serverCapabilitiesAggregateFunctions();
    }

    if (!this.aggregateFunctions.includes(aggregate_fn)) {
      throw new Error(
        this.aggregateFunctions.length === 0
          ? "Server does not advertise any aggregate functions"
          : `Invalid aggregate function. Supported: ${this.aggregateFunctions.join(", ")}`
      );
    }

    try {
      const aggregateFn = AggregateFunction[aggregate_fn as keyof typeof AggregateFunction];
      const historyValues = await this.session.readAggregateValue(
        { nodeId },
        toDate(start) as any,
        (toDate(end) ?? new Date()) as any,
        aggregateFn,
        processing_interval
      );
      if (historyValues.statusCode !== StatusCodes.Good) {
        throw new Error(
          `Read aggregate failed with status: ${historyValues.statusCode.toString()}`
        );
      }
      const dataValues = (historyValues.historyData as HistoryData).dataValues;
      return historyResult(dataValues);
    } catch (error) {
      throw new Error(
        `Failed to read node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async writeOpcuaNode(nodeId: string, value: unknown) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      // First read the current value to determine the data type
      const currentDataValue = await this.session.readVariableValue(nodeId);

      if (currentDataValue.statusCode !== StatusCodes.Good || !currentDataValue.value) {
        throw new Error(`Cannot read target data type: ${currentDataValue.statusCode.toString()}`);
      }
      const target = currentDataValue.value;
      const convertedValue = convertForVariant(value, target.dataType, target.arrayType);

      const nodeToWrite = {
        nodeId: nodeId,
        attributeId: AttributeIds.Value,
        value: new DataValue({
          value: new Variant({
            dataType: target.dataType,
            arrayType: target.arrayType,
            dimensions: target.dimensions,
            value: convertedValue,
          }),
        }),
      };

      const statusCode = await this.session.write(nodeToWrite);

      if (statusCode !== StatusCodes.Good) {
        throw new Error(`Write failed with status: ${statusCode.toString()}`);
      }

      return {
        content: [
          {
            type: "text",
            text: `Successfully wrote ${value} to node ${nodeId}`,
          },
        ],
      };
    } catch (error) {
      throw new Error(
        `Failed to write to node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async browseOpcuaNodeChildren(nodeId: string) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      const references = await browseAllReferences(this.session, nodeId);

      const childrenInfo = references.map((ref: ReferenceDescription) => ({
        node_id: ref.nodeId.toString(),
        browse_name: `${ref.browseName.namespaceIndex}:${ref.browseName.name}`,
      }));

      return {
        content: [
          {
            type: "text",
            text: `Children of ${nodeId}: ${JSON.stringify(childrenInfo, null, 2)}`,
          },
        ],
      };
    } catch (error) {
      throw new Error(
        `Failed to browse children of node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async readMultipleOpcuaNodes(nodeIds: string[]) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      const nodesToRead = nodeIds.map((nodeId) => ({
        nodeId: nodeId,
        attributeId: AttributeIds.Value,
      }));

      const dataValues = await this.session.read(nodesToRead);

      const results: { [key: string]: any } = {};

      dataValues.forEach((dataValue, index) => {
        const nodeId = nodeIds[index];
        if (dataValue.statusCode === StatusCodes.Good) {
          results[nodeId] = dataValue.value?.value;
        } else {
          results[nodeId] = `Error: ${dataValue.statusCode.toString()}`;
        }
      });

      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(results, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new Error(
        `Failed to read multiple nodes: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async writeMultipleOpcuaNodes(nodesToWrite: Array<{ node_id: string; value: unknown }>) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      // First, read current values to determine data types
      const nodeIds = nodesToWrite.map((item) => item.node_id);
      const nodesToRead = nodeIds.map((nodeId) => ({
        nodeId: nodeId,
        attributeId: AttributeIds.Value,
      }));

      const currentDataValues = await this.session.read(nodesToRead);

      const results: Array<{ node_id: string; status: string } | undefined> = new Array(
        nodesToWrite.length
      );
      const writeNodes: Array<{
        nodeId: string;
        attributeId: AttributeIds;
        value: DataValue;
      }> = [];
      const writeIndices: number[] = [];

      nodesToWrite.forEach((item, index) => {
        const currentDataValue = currentDataValues[index];
        if (currentDataValue.statusCode !== StatusCodes.Good || !currentDataValue.value) {
          results[index] = {
            node_id: item.node_id,
            status: `Error: ${currentDataValue.statusCode.toString()}`,
          };
          return;
        }
        const target = currentDataValue.value;
        try {
          const convertedValue = convertForVariant(item.value, target.dataType, target.arrayType);
          writeNodes.push({
            nodeId: item.node_id,
            attributeId: AttributeIds.Value,
            value: new DataValue({
              value: new Variant({
                dataType: target.dataType,
                arrayType: target.arrayType,
                dimensions: target.dimensions,
                value: convertedValue,
              }),
            }),
          });
          writeIndices.push(index);
        } catch (error) {
          results[index] = {
            node_id: item.node_id,
            status: `Error: ${error instanceof Error ? error.message : String(error)}`,
          };
        }
      });

      if (writeNodes.length > 0) {
        const statusCodes = await this.session.write(writeNodes);
        statusCodes.forEach((statusCode, resultIndex) => {
          const inputIndex = writeIndices[resultIndex];
          results[inputIndex] = {
            node_id: nodesToWrite[inputIndex].node_id,
            status: statusCode === StatusCodes.Good ? "Success" : `Error: ${statusCode.toString()}`,
          };
        });
      }

      return {
        content: [
          {
            type: "text",
            text: `Write operation results:\n${JSON.stringify(results, null, 2)}`,
          },
        ],
      };
    } catch (error) {
      throw new Error(
        `Failed to write multiple nodes: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  private async callOpcuaMethod(objectNodeId: string, methodNodeId: string, methodArgs?: string[]) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      // Convert string arguments to appropriate types
      const convertedArgs: Variant[] = [];

      if (methodArgs) {
        for (const arg of methodArgs) {
          // Try to convert to appropriate type
          let convertedValue: any;

          // Try float first
          const floatValue = parseFloat(arg);
          if (!isNaN(floatValue)) {
            convertedValue = floatValue;
          } else {
            // Try int
            const intValue = parseInt(arg);
            if (!isNaN(intValue)) {
              convertedValue = intValue;
            } else {
              // Keep as string
              convertedValue = arg;
            }
          }

          convertedArgs.push(
            new Variant({
              dataType: typeof convertedValue === "number" ? DataType.Double : DataType.String,
              value: convertedValue,
            })
          );
        }
      }

      const methodToCall = {
        objectId: objectNodeId,
        methodId: methodNodeId,
        inputArguments: convertedArgs,
      };

      const callResult: CallMethodResult = await this.session.call(methodToCall);

      if (callResult.statusCode !== StatusCodes.Good) {
        throw new Error(`Method call failed with status: ${callResult.statusCode.toString()}`);
      }

      return {
        content: [
          {
            type: "text",
            text: `Method call successful. Object: ${objectNodeId}, Method: ${methodNodeId}, Result: ${JSON.stringify(callResult.outputArguments)}`,
          },
        ],
      };
    } catch (error) {
      throw new Error(
        `Failed to call method ${methodNodeId} on object ${objectNodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  // --- events and Alarms & Conditions ------------------------------------------
  // The wording of every message below is shared with the Python server's
  // `events` tools, so a model that has learned one runtime's replies reads the
  // other's the same way. See packages/server-python/.../server.py.

  private async subscribeEvents(nodeId: string, severityMin: number, bufferSize: number) {
    try {
      await this.events.subscribe(this.requireSession(), nodeId, severityMin, bufferSize);
    } catch (error) {
      throw new Error(
        `Failed to subscribe to events from node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }

    return {
      content: [
        {
          type: "text",
          text:
            `Subscribed to events from node ${nodeId}, buffering up to ${bufferSize} ` +
            `events of severity ${severityMin} or above. Read them with read_events.`,
        },
      ],
    };
  }

  private readEvents(nodeId: string, limit: number) {
    const drained = this.events.drain(this.requireSession(), nodeId, limit);
    if (drained === null) {
      throw new Error(`Not subscribed to events from node ${nodeId}. Call subscribe_events first.`);
    }
    const result = eventResult(drained.records);
    if (drained.dropped > 0) {
      // In the response, not only on stderr: an agent that cannot tell a
      // complete event stream from one that lost alarms reads the gap as quiet.
      result.content.push({
        type: "text",
        text: droppedEventsMessage(drained.dropped, drained.size),
      });
    }
    return result;
  }

  private async listActiveAlarms(nodeId: string, timeoutSeconds: number) {
    let alarms: EventRecord[];
    try {
      alarms = await listActiveAlarms(this.requireSession(), nodeId, timeoutSeconds);
    } catch (error) {
      throw new Error(
        `Failed to list active alarms from node ${nodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }

    this.events.remember(alarms);
    return eventResult(alarms);
  }

  private async acknowledgeAlarm(eventId: string, comment: string, conditionId?: string) {
    const condition = conditionId || this.events.conditionFor(eventId);
    if (!condition) {
      throw new Error(
        `Unknown event_id "${eventId}". Call list_active_alarms first, or pass the ` +
          "condition_id of the alarm to acknowledge."
      );
    }

    let statusCode;
    try {
      statusCode = await acknowledgeAlarm(this.requireSession(), condition, eventId, comment);
    } catch (error) {
      throw new Error(
        `Failed to acknowledge alarm ${condition}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
    if (statusCode !== StatusCodes.Good) {
      throw new Error(`Failed to acknowledge alarm ${condition}: ${statusCode.toString()}`);
    }

    return {
      content: [
        {
          type: "text",
          text: `Acknowledged alarm ${condition} (event ${eventId})`,
        },
      ],
    };
  }

  private async getAllVariables(
    rootNodeId = "ns=0;i=85",
    requestedMaxDepth = 8,
    requestedMaxNodes = 500,
    includeValues = true
  ) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      const variablesInfo: Array<{
        name: string;
        nodeid: string;
        object_id: string;
        value: any;
        data_type: string;
        description: string;
      }> = [];

      const maxDepth = Math.max(0, Math.min(Math.trunc(requestedMaxDepth), 64));
      const maxNodes = Math.max(1, Math.min(Math.trunc(requestedMaxNodes), 5000));
      const queue: Array<{ nodeId: string; depth: number }> = [{ nodeId: rootNodeId, depth: 0 }];
      const visited = new Set<string>([rootNodeId]);
      let inspected = 0;
      let truncated = false;

      while (queue.length > 0 && !truncated) {
        const { nodeId, depth } = queue.shift()!;
        try {
          // Same drain as `browse_opcua_node_children`, and deliberately the
          // same helper: a traversal that browses short is the bug this tool
          // would hide best, because nobody counts a plant's variables by hand.
          const references = await browseAllReferences(this.session!, nodeId);

          for (const ref of references) {
            try {
              const childNodeId = ref.nodeId.toString();
              if (visited.has(childNodeId)) {
                continue;
              }
              visited.add(childNodeId);
              if (inspected >= maxNodes) {
                truncated = true;
                break;
              }
              inspected += 1;
              const browseName = ref.browseName.name;

              // Skip the entire "Server" subtree
              if (browseName === "Server") {
                continue;
              }

              // Read the node class to determine if it's a variable or object
              const nodeClassResults = await this.session!.read({
                nodeId: childNodeId,
                attributeId: AttributeIds.NodeClass,
              });

              const nodeClass = nodeClassResults.value?.value;

              if (nodeClass === 2) {
                // NodeClass.Variable = 2
                // This is a variable node
                let value: any;
                let dataType = "";
                let description = "";
                const objectId = nodeId;

                if (includeValues) {
                  try {
                    const valueResult = await this.session!.readVariableValue(childNodeId);
                    value = valueResult.value?.value;
                  } catch {
                    value = null;
                  }
                } else {
                  value = null;
                }

                try {
                  const dataTypeResults = await this.session!.read({
                    nodeId: childNodeId,
                    attributeId: AttributeIds.DataType,
                  });
                  dataType = dataTypeResults.value?.value?.toString() || "";
                } catch {
                  dataType = "";
                }

                try {
                  const descResults = await this.session!.read({
                    nodeId: childNodeId,
                    attributeId: AttributeIds.Description,
                  });
                  description = descResults.value?.value?.text || "";
                } catch {
                  description = "";
                }

                variablesInfo.push({
                  name: browseName || "",
                  nodeid: childNodeId,
                  object_id: objectId,
                  value: value,
                  data_type: dataType,
                  description: description,
                });
              } else if (nodeClass === 1 && depth < maxDepth) {
                // NodeClass.Object = 1
                queue.push({ nodeId: childNodeId, depth: depth + 1 });
              }
            } catch (error) {
              // Continue with next reference if this one fails
              console.error(`Error processing reference: ${error}`);
            }
          }
        } catch (error) {
          if (nodeId === rootNodeId) throw error;
          // Continue if browse fails for this node
          console.error(`Error browsing node ${nodeId}: ${error}`);
        }
      }

      if (variablesInfo.length > 0) {
        let result = `Found ${variablesInfo.length} variables after inspecting ${inspected} nodes`;
        if (truncated) {
          result += ` (truncated at max_nodes=${maxNodes})`;
        }
        result += ":\n";
        for (const variable of variablesInfo) {
          result += `\n- Name: ${variable.name}\n`;
          result += `  NodeID: ${variable.nodeid}\n`;
          result += `  Object ID: ${variable.object_id}\n`;
          result += `  Value: ${variable.value}\n`;
          result += `  Data Type: ${variable.data_type}\n`;
          result += `  Description: ${variable.description}\n`;
        }

        return {
          content: [
            {
              type: "text",
              text: result,
            },
          ],
        };
      } else {
        let result = `No variables found after inspecting ${inspected} nodes`;
        if (truncated) {
          result += ` (truncated at max_nodes=${maxNodes})`;
        }
        return {
          content: [
            {
              type: "text",
              text: `${result}.`,
            },
          ],
        };
      }
    } catch (error) {
      throw new Error(
        `Failed to discover variables below ${rootNodeId}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
}
