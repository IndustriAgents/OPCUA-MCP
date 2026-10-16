/** A control uses the exact session and authorization selected for its attempt. */
import { AsyncLocalStorage } from "node:async_hooks";
import { ContractRefusal, message } from "../errors.js";
import type { ExecutionCall } from "../application/execution.js";
import type { OpcuaConnection } from "../connection.js";
import type { ToolPolicy } from "../policy.js";

const current = new AsyncLocalStorage<() => void>();

export function dispatchControl<T>(operation: () => T): T {
  current.getStore()?.();
  return operation();
}

export function runControl<T>(
  call: ExecutionCall,
  connection: OpcuaConnection,
  policy: ToolPolicy,
  operation: () => T,
  signal?: AbortSignal
): T {
  return current.run(() => {
    if (signal?.aborted) throw new ContractRefusal(message("controlCancelled"));
    if (!connection.connected || connection.sessionId !== call.session)
      throw new ContractRefusal(message("sessionChanged", { tool: call.name }));
    policy.authorize(call.name, call.arguments);
  }, operation);
}
