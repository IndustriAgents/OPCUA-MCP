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
  BrowseResult,
  ReferenceDescription,
  HistoryData,
  AggregateFunction,
  ClientSession,
} from "node-opcua-client";
import { Resource, Tool } from "@modelcontextprotocol/sdk/types.js";

import { OpcuaConnection } from "./connection.js";
import { CONTRACT } from "./contract.js";
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
import { SubscriptionManager, SubscriptionRecord } from "./subscriptions.js";

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

function recordBlocks(records: unknown[]) {
  return {
    content: records.map((record) => ({
      type: "text",
      text: JSON.stringify(record, null, 2),
    })),
  };
}

export class OpcuaTools {
  private aggregateFunctions: string[] = [];
  private readonly subs = new SubscriptionManager();
  private readonly events = new EventSubscriptions();

  constructor(private readonly conn: OpcuaConnection) {}

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
    const historyOk = await this.accessHistoryDataCapability();
    this.aggregateFunctions = await this.serverCapabilitiesAggregateFunctions();
    const aggregateOk = this.aggregateFunctions.length > 0;

    const tools = CONTRACT.tools
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
          return { name: t.name, description: t.description, inputSchema };
        }
        return { name: t.name, description: t.description, inputSchema: t.inputSchema };
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

  /** Dispatch a tools/call request by name. */
  async callTool(request: { params: { name: string; arguments?: Record<string, unknown> } }) {
    const { name, arguments: args } = request.params;

    try {
      await this.ensureConnection();

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
          return await this.writeOpcuaNode(args?.node_id as string, args?.value as string);

        case "browse_opcua_node_children":
          return await this.browseOpcuaNodeChildren(args?.node_id as string);

        case "read_multiple_opcua_nodes":
          return await this.readMultipleOpcuaNodes(args?.node_ids as string[]);

        case "write_multiple_opcua_nodes":
          return await this.writeMultipleOpcuaNodes(
            args?.nodes_to_write as Array<{ node_id: string; value: string }>
          );

        case "call_opcua_method":
          return await this.callOpcuaMethod(
            args?.object_node_id as string,
            args?.method_node_id as string,
            args?.arguments as string[]
          );

        case "get_all_variables":
          return await this.getAllVariables();

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
    } catch (error) {
      return {
        content: [
          {
            type: "text",
            text: `Error: ${error instanceof Error ? error.message : String(error)}`,
          },
        ],
      };
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

  private async writeOpcuaNode(nodeId: string, value: string) {
    if (!this.session) {
      throw new Error("No OPC UA session available");
    }

    try {
      // First read the current value to determine the data type
      const currentDataValue = await this.session.readVariableValue(nodeId);

      let convertedValue: any;
      const currentValue = currentDataValue.value?.value;
      // Coerce to string first: a client may send a non-string (e.g. boolean/number) value.
      const valueStr = String(value);

      // Convert value based on the current type
      if (typeof currentValue === "number") {
        convertedValue = parseFloat(valueStr);
        if (isNaN(convertedValue)) {
          throw new Error(`Cannot convert "${valueStr}" to number`);
        }
      } else if (typeof currentValue === "boolean") {
        convertedValue = valueStr.toLowerCase() === "true" || valueStr === "1";
      } else {
        convertedValue = valueStr; // Keep as string
      }

      const nodeToWrite = {
        nodeId: nodeId,
        attributeId: AttributeIds.Value,
        value: new DataValue({
          value: new Variant({
            dataType: currentDataValue.value?.dataType || DataType.String,
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
      const browseResult = await this.session.browse(nodeId);

      if (browseResult.statusCode !== StatusCodes.Good) {
        throw new Error(`Browse failed with status: ${browseResult.statusCode.toString()}`);
      }

      const childrenInfo =
        browseResult.references?.map((ref: ReferenceDescription) => ({
          node_id: ref.nodeId.toString(),
          browse_name: `${ref.browseName.namespaceIndex}:${ref.browseName.name}`,
        })) || [];

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

  private async writeMultipleOpcuaNodes(nodesToWrite: Array<{ node_id: string; value: string }>) {
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

      const writeNodes = nodesToWrite.map((item, index) => {
        const currentDataValue = currentDataValues[index];
        const currentValue = currentDataValue.value?.value;

        let convertedValue: any;
        // Coerce to string first: a client may send a non-string (e.g. boolean/number) value.
        const valueStr = String(item.value);

        // Convert value based on the current type
        if (typeof currentValue === "number") {
          convertedValue = parseFloat(valueStr);
          if (isNaN(convertedValue)) {
            throw new Error(`Cannot convert "${valueStr}" to number for node ${item.node_id}`);
          }
        } else if (typeof currentValue === "boolean") {
          convertedValue = valueStr.toLowerCase() === "true" || valueStr === "1";
        } else {
          convertedValue = valueStr; // Keep as string
        }

        return {
          nodeId: item.node_id,
          attributeId: AttributeIds.Value,
          value: new DataValue({
            value: new Variant({
              dataType: currentDataValue.value?.dataType || DataType.String,
              value: convertedValue,
            }),
          }),
        };
      });

      const statusCodes = await this.session.write(writeNodes);

      const results = statusCodes.map((statusCode, index) => ({
        node_id: nodesToWrite[index].node_id,
        status: statusCode === StatusCodes.Good ? "Success" : `Error: ${statusCode.toString()}`,
      }));

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

  private async getAllVariables() {
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

      // Start browsing from the Objects folder (ns=0;i=85)
      const objectsNodeId = "ns=0;i=85";

      const searchVariables = async (nodeId: string): Promise<void> => {
        try {
          const browseResult = await this.session!.browse(nodeId);

          if (browseResult.statusCode !== StatusCodes.Good || !browseResult.references) {
            return;
          }

          for (const ref of browseResult.references) {
            try {
              const childNodeId = ref.nodeId.toString();
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
                let objectId = nodeId;

                try {
                  const valueResult = await this.session!.readVariableValue(childNodeId);
                  value = valueResult.value?.value;
                } catch {
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
              } else if (nodeClass === 1) {
                // NodeClass.Object = 1
                // This is an object node, recursively search its children
                await searchVariables(childNodeId);
              }
            } catch (error) {
              // Continue with next reference if this one fails
              console.error(`Error processing reference: ${error}`);
            }
          }
        } catch (error) {
          // Continue if browse fails for this node
          console.error(`Error browsing node ${nodeId}: ${error}`);
        }
      };

      await searchVariables(objectsNodeId);

      if (variablesInfo.length > 0) {
        let result = `Found ${variablesInfo.length} variables:\n`;
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
        return {
          content: [
            {
              type: "text",
              text: "No variables found in the OPC UA server.",
            },
          ],
        };
      }
    } catch (error) {
      throw new Error(
        `Failed to get all variables: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
}
