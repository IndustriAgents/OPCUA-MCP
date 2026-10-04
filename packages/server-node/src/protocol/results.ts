/** MCP result framing, notices and advertised output schemas. */
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import { CONTRACT, type ToolSpec } from "../contract.js";
import type { Completeness } from "../completeness.js";
import { bufferCompleteness } from "../completeness.js";
import { notice } from "../notices.js";
import { prettyJson } from "../result-text.js";
import type { SubscriptionRecord } from "../application/subscriptions.js";
import type { ServerStatusRecord } from "../application/diagnostics.js";
import type { CapabilityStatusRecord } from "../capabilities.js";
export type ServerStatusReport = ServerStatusRecord & { capabilities: CapabilityStatusRecord };
export function withNotice<T extends { content: Array<{ type: string; text: string }> }>(
  result: T,
  text: string | null
): T {
  if (text === null) return result;
  return { ...result, content: [...result.content, { type: "text", text }] };
}

export function historyResult(records: unknown[], completeness: Completeness, capNotice: string) {
  let text: string | null = null;
  if (completeness.reasons.includes("contractLimit")) {
    // The cap, not the count returned: an event-history read reaches its cap on
    // what the server sent, and `severity_min` may have kept fewer.
    text = notice(capNotice, { count: completeness.limit ?? completeness.returned });
  } else if (completeness.reasons.includes("serverLimit")) {
    text = notice("serverTruncated", { count: completeness.returned });
  }
  return withNotice(recordBlocks(records, completeness), text);
}

export function subscriptionResult(records: SubscriptionRecord[]) {
  return recordBlocks(records, bufferCompleteness(records));
}

export function eventResult(records: unknown[], completeness?: Completeness) {
  return recordBlocks(records, completeness);
}

export function objectResult(record: unknown, completeness?: Completeness) {
  return {
    content: [{ type: "text", text: prettyJson(record) }],
    structuredContent: completeness ? { result: record, completeness } : { result: record },
  };
}

export function statusResult(status: ServerStatusReport) {
  return objectResult(status);
}

export function recordBlocks(records: unknown[], completeness?: Completeness) {
  return {
    content: records.map((record) => ({
      type: "text",
      text: prettyJson(record),
    })),
    // Beside `result`, never inside it: `result` stays the array every existing
    // client already reads, and a record list that sometimes ends in something
    // that is not a record would be worse than the prose it replaces.
    structuredContent: completeness ? { result: records, completeness } : { result: records },
  };
}

export function outputSchema(tool: ToolSpec): Tool["outputSchema"] {
  if (!tool.resultShape) return undefined;
  if (!tool.reportsCompleteness) {
    return {
      type: "object",
      properties: { result: CONTRACT.resultShapes[tool.resultShape] },
      required: ["result"],
      additionalProperties: false,
    } as Tool["outputSchema"];
  }
  return {
    type: "object",
    properties: {
      result: CONTRACT.resultShapes[tool.resultShape],
      completeness: CONTRACT.completeness.schema,
    },
    required: ["result", "completeness"],
    additionalProperties: false,
  } as Tool["outputSchema"];
}

export type ProtocolResult = ReturnType<typeof objectResult>;
