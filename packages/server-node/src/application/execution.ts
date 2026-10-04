/** Common authorization/audit envelope; transport and native recovery are injected. */
import { CONTRACT, type ToolSpec } from "../contract.js";
import { ContractRefusal, describeError } from "../errors.js";
import { message } from "../errors.js";
import { checkRequestBounds } from "../limits.js";
import { validateArguments } from "../validation.js";
export interface ExecutionCall {
  name: string;
  arguments: Record<string, unknown>;
  spec: ToolSpec;
  callId: string;
  attempt: number;
  denied: boolean;
  session: string | null;
}
export interface ExecutionPort<R> {
  newCallId(): string;
  waitForConnection(): Promise<void>;
  authorize(name: string, args: Record<string, unknown>): void;
  allowed(call: ExecutionCall): void;
  after(call: ExecutionCall, decision: "denied" | "failed" | "completed", reason?: string): void;
  run(call: ExecutionCall): Promise<R>;
  normalizeFailure(name: string, error: unknown): unknown;
  normalizeResult(result: R): R;
}
export async function executeTool<R>(
  port: ExecutionPort<R>,
  name: string,
  args: Record<string, unknown>
): Promise<R> {
  const callId = port.newCallId();
  const spec = CONTRACT.tools.find((tool) => tool.name === name);
  if (!spec) throw new ContractRefusal(message("unknownTool", { tool: name }));
  const call: ExecutionCall = {
    name,
    arguments: args,
    spec,
    callId,
    attempt: 1,
    denied: false,
    session: null,
  };
  try {
    checkRequestBounds(name, args);
    validateArguments(name, spec.inputSchema, args);
    if (name !== "get_server_status") await port.waitForConnection();
    port.authorize(name, args);
  } catch (error) {
    call.denied = true;
    port.after(call, "denied", describeError(error));
    throw error;
  }
  port.allowed(call);
  try {
    const result = await port.run(call);
    port.after(call, "completed");
    return port.normalizeResult(result);
  } catch (error) {
    const reported = port.normalizeFailure(name, error);
    if (!call.denied) port.after(call, "failed", describeError(reported));
    throw reported;
  }
}
