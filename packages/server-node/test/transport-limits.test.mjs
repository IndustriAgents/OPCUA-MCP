// The transport bounds this client asks node-opcua to enforce (CVE-2022-25304).
//
// node-opcua enforces these itself, so unlike the Python half there is nothing
// to patch here. What matters is that the *numbers* are the same numbers: a
// limit one runtime applies and the other does not is a difference in what the
// two are safe against. `tests/unit/test_transport_limits.py` asserts the same
// values against the same contract block, and drives the attack through
// python-opcua's real reassembly.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { CONTRACT } from "../build/contract.js";
import {
  MAX_CHUNK_COUNT,
  MAX_CHUNK_SIZE,
  MAX_MESSAGE_SIZE,
  transportSettings,
} from "../build/transport-limits.js";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

describe("transport limits", () => {
  it("takes its numbers from the contract", () => {
    assert.equal(MAX_CHUNK_COUNT, CONTRACT.transport.maxChunkCount);
    assert.equal(MAX_CHUNK_SIZE, CONTRACT.transport.maxChunkSize);
    assert.equal(MAX_MESSAGE_SIZE, CONTRACT.transport.maxMessageSize);
  });

  it("keeps the message bound consistent with the chunk bounds", () => {
    // Otherwise the two limits contradict each other and the looser one is the
    // real one.
    assert.equal(MAX_CHUNK_COUNT * MAX_CHUNK_SIZE, MAX_MESSAGE_SIZE);
  });

  it("hands node-opcua both bounds", () => {
    const settings = transportSettings();
    assert.equal(settings.maxChunkCount, MAX_CHUNK_COUNT);
    assert.equal(settings.maxMessageSize, MAX_MESSAGE_SIZE);
    assert.equal(settings.receiveBufferSize, MAX_CHUNK_SIZE);
  });

  it("is actually applied by the client factory", () => {
    // The settings are useless if nothing passes them to the client that
    // connects, and that is not something the type checker will notice.
    const connection = readFileSync(join(SRC, "connection.ts"), "utf8");
    assert.match(connection, /transportSettings: transportSettings\(\)/);
  });
});
