import assert from "node:assert/strict";
import { test } from "node:test";
import { CallRequest, WriteRequest, ReadRequest, NodeId } from "node-opcua-client";
import { ClientSessionImpl } from "node-opcua-client/dist/private/client_session_impl.js";
import { singleSendControls } from "../build/adapters/control-transactions.js";

for (const Request of [WriteRequest, CallRequest]) {
  test(`${Request.name} fails a broken channel without being queued`, () => {
    const session = Object.create(ClientSessionImpl.prototype);
    session._client = { isReconnecting: true };
    session._reconnecting = { reconnecting: true, pendingTransactions: [] };
    session.isChannelValid = () => false;
    singleSendControls(session);
    let outcome;
    session.performMessageTransaction(new Request(), (error) => {
      outcome = error;
    });
    assert.match(outcome.message, /BadConnectionClosed/);
    assert.equal(session._reconnecting.pendingTransactions.length, 0);
  });
  test(`${Request.name} returns BadSessionIdInvalid without native repair/replay`, () => {
    const session = Object.create(ClientSessionImpl.prototype);
    let sends = 0;
    session._client = {
      performMessageTransaction(request, callback) {
        sends++;
        callback(new Error("BadSessionIdInvalid"));
      },
    };
    session.authenticationToken = new NodeId(1, 1);
    session.isChannelValid = () => true;
    session._reconnecting = { reconnecting: false, pendingTransactions: [] };
    singleSendControls(session);
    let outcome;
    session.performMessageTransaction(new Request(), (error) => {
      outcome = error;
    });
    assert.match(outcome.message, /BadSessionIdInvalid/);
    assert.equal(sends, 1);
    assert.equal(session._reconnecting.pendingTransactions.length, 0);
  });
}
test("read transactions keep native recovery", () => {
  let reads = 0;
  const session = {
    performMessageTransaction(request, callback) {
      reads++;
      callback(null, "read");
    },
    _performMessageTransaction() {
      assert.fail("control boundary used for read");
    },
  };
  singleSendControls(session);
  session.performMessageTransaction(new ReadRequest(), (error, result) => {
    assert.equal(error, null);
    assert.equal(result, "read");
  });
  assert.equal(reads, 1);
});
test("a client without the single-send native primitive fails closed", () => {
  assert.throws(() => singleSendControls({}), /single-send control transaction boundary/);
});
