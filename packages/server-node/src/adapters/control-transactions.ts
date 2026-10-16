/** Controls cannot use node-opcua's transparent session repair and request replay. */
import { WriteRequest, CallRequest, type ClientSession } from "node-opcua-client";
import { dispatchControl } from "../infrastructure/control-context.js";

type Transaction = (
  request: unknown,
  callback: (error: Error | null, response?: unknown) => void
) => void;
type NativeSession = ClientSession & {
  performMessageTransaction: Transaction;
  _performMessageTransaction: Transaction;
};

export function singleSendControls(session: ClientSession): void {
  const native = session as NativeSession;
  if (typeof native._performMessageTransaction !== "function")
    throw new Error("The OPC UA client lacks the single-send control transaction boundary");
  const original = native.performMessageTransaction.bind(session);
  native.performMessageTransaction = (request, callback) => {
    if (!(request instanceof WriteRequest) && !(request instanceof CallRequest)) {
      original(request, callback);
      return;
    }
    try {
      // Recheck at the transaction boundary too, before authenticationToken is assigned.
      // The native primitive rejects a broken channel; it neither queues nor repairs/replays.
      dispatchControl(() => native._performMessageTransaction(request, callback));
    } catch (error) {
      callback(error instanceof Error ? error : new Error(String(error)));
    }
  };
}
