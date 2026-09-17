// Connection resilience: the retry settings, and what counts as a dead session.
//
// The Python server's `tests/unit/test_reconnect.py` asserts the same behaviour
// and additionally compares the two implementations' answers directly. This file
// is the Node-side detail: the parsing rules, and the reasoning about which
// errors are worth reconnecting for.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  RECONNECT_DEFAULTS,
  describeReconnect,
  parseReconnectConfig,
  reconnectBudgetMs,
} from "../build/config.js";
import { isConnectionError, notConnectedMessage } from "../build/connection.js";
import { CONTRACT } from "../build/contract.js";

describe("reconnect settings", () => {
  it("defaults to retrying, without hanging a tool call", () => {
    assert.deepEqual(parseReconnectConfig({}), RECONNECT_DEFAULTS);
    assert.ok(RECONNECT_DEFAULTS.maxRetry > 0, "a dropped connection must be retried by default");
    assert.ok(reconnectBudgetMs(RECONNECT_DEFAULTS) <= 15000);
  });

  it("treats an empty value as unset", () => {
    // An MCP client config that writes `"OPCUA_RECONNECT_MAX_RETRY": ""` means
    // "leave it alone", not "zero".
    assert.deepEqual(parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: "  " }), RECONNECT_DEFAULTS);
  });

  it("refuses a setting it cannot honour", () => {
    for (const [name, value] of [
      ["OPCUA_RECONNECT_INITIAL_DELAY_MS", "soon"],
      ["OPCUA_RECONNECT_INITIAL_DELAY_MS", "-1"],
      ["OPCUA_RECONNECT_MAX_DELAY_MS", "-5"],
      ["OPCUA_RECONNECT_MAX_RETRY", "many"],
      ["OPCUA_RECONNECT_MAX_RETRY", "-2"],
      ["OPCUA_SESSION_TIMEOUT_MS", "10"],
    ]) {
      assert.throws(
        () => parseReconnectConfig({ [name]: value }),
        new RegExp(name),
        `${name}=${value}`
      );
    }
  });

  it("spells unlimited retries as -1", () => {
    assert.equal(parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: "-1" }).maxRetry, -1);
    assert.match(
      describeReconnect(parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: "-1" })),
      /unlimited/
    );
  });

  it("waits the doubling sequence it configured, and no longer", () => {
    // 250 + 500 + 1000 + 1000 + 1000, the ceiling holding after the third.
    const config = { initialDelay: 250, maxDelay: 1000, maxRetry: 5, sessionTimeout: 60000 };
    assert.equal(reconnectBudgetMs(config), 3750);
  });

  it("gives an unlimited retry a finite budget", () => {
    // A tool call waits out this budget before rebuilding the client itself, so
    // "forever" here would mean a request that never returns.
    const budget = reconnectBudgetMs({ ...RECONNECT_DEFAULTS, maxRetry: -1 });
    assert.ok(Number.isFinite(budget) && budget > 0);
  });

  it("still waits once when retrying is switched off", () => {
    // maxRetry 0 hands node-opcua a single attempt; the budget is the floor
    // rather than zero so a repair in flight is not abandoned instantly.
    assert.equal(
      reconnectBudgetMs({ ...RECONNECT_DEFAULTS, maxRetry: 0 }),
      RECONNECT_DEFAULTS.initialDelay
    );
  });

  it("describes the settings for the startup log", () => {
    assert.equal(
      describeReconnect({ initialDelay: 250, maxDelay: 1000, maxRetry: 8, sessionTimeout: 30000 }),
      "retries=8 backoff=250..1000ms session-timeout=30000ms"
    );
  });
});

describe("dead-session detection", () => {
  it("looks inside a rewrapped message", () => {
    // Tool bodies re-throw as prose, so the status code arrives embedded.
    assert.ok(isConnectionError(new Error("Failed to read node ns=2;i=3: BadSessionIdInvalid")));
    assert.ok(isConnectionError(new Error("connect ECONNREFUSED 127.0.0.1:4840")));
  });

  it("does not mistake a failed request for a failed connection", () => {
    // Retrying any of these on a fresh session would fail the same way.
    for (const message of [
      "Failed to read node ns=2;i=999999: BadNodeIdUnknown",
      'Invalid date/time: "nope". Use ISO 8601, e.g. 2026-04-23T17:40:00Z',
      "BadOutOfRange",
      "",
    ]) {
      assert.equal(isConnectionError(new Error(message)), false, message);
    }
  });

  it("words the not-connected error the way both runtimes word it", () => {
    assert.equal(
      notConnectedMessage("opc.tcp://plc:4840", "ECONNREFUSED"),
      "Not connected to the OPC UA server at opc.tcp://plc:4840: ECONNREFUSED. " +
        "Call get_server_status for details."
    );
  });
});

describe("the diagnostics tool", () => {
  it("is a read tool with no capability gate", () => {
    // ServerStatus is mandatory in OPC UA, and a status report that an
    // observe-only deployment could not call would be useless precisely when it
    // is most needed.
    const tool = CONTRACT.tools.find((candidate) => candidate.name === "get_server_status");
    assert.ok(tool, "get_server_status is missing from the contract");
    assert.equal(tool.accessClass, "read");
    assert.equal(tool.capability, null);
    assert.equal(tool.annotations.readOnlyHint, true);
    assert.equal(tool.resultShape, "serverStatus");
  });
});
