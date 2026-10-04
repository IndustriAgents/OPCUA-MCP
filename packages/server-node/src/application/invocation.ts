/** One connected attempt and contract-directed recovery through injected services. */
import type { ExecutionCall } from "./execution.js";
import { ContractRefusal, describeError, message } from "../errors.js";
export interface InvocationPort<R> {
  endpoint(): string;
  session(): string | null;
  hasConnection(): boolean;
  waitForWarmUp(): Promise<void>;
  connect(): Promise<void>;
  capabilities(call: ExecutionCall): Promise<void>;
  dispatch(call: ExecutionCall): Promise<R>;
  isConnectionError(error: unknown): boolean;
  reconnect(session: string | null): Promise<void>;
  logRecovery(resend: boolean): void;
  targets(call: ExecutionCall): string;
  authorize(call: ExecutionCall): void;
  allowed(call: ExecutionCall): void;
  denied(call: ExecutionCall, reason: string): void;
}
export async function invokeTool<R>(port: InvocationPort<R>, call: ExecutionCall): Promise<R> {
  if (call.name === "get_server_status" || !port.hasConnection()) {
    await port.waitForWarmUp();
    return await port.dispatch(call);
  }
  try {
    await port.connect();
  } catch (error) {
    throw new ContractRefusal(
      message("notConnected", { url: port.endpoint(), reason: describeError(error) }),
      { cause: error }
    );
  }
  call.session = port.session();
  await port.capabilities(call);
  try {
    return await port.dispatch(call);
  } catch (error) {
    if (!port.isConnectionError(error)) throw error;
    return await recover(port, call, error);
  }
}
async function recover<R>(
  port: InvocationPort<R>,
  call: ExecutionCall,
  error: unknown
): Promise<R> {
  const retry = call.spec.retryPolicy;
  port.logRecovery(retry === "resend");
  try {
    await port.reconnect(call.session);
  } catch (error) {
    throw new ContractRefusal(
      message("notConnected", { url: port.endpoint(), reason: describeError(error) }),
      { cause: error }
    );
  }
  if (retry === "uncertainOutcome")
    throw new ContractRefusal(
      message("uncertainOutcome", {
        tool: call.name,
        reason: describeError(error),
        targets: port.targets(call),
      }),
      { cause: error }
    );
  if (retry !== "resend") throw error;
  call.attempt = 2;
  try {
    port.authorize(call);
  } catch (error) {
    call.denied = true;
    port.denied(call, describeError(error));
    throw error;
  }
  try {
    port.allowed(call);
  } catch (error) {
    call.denied = true;
    throw error;
  }
  await port.capabilities(call);
  return await port.dispatch(call);
}
