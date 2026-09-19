/** Bounds on what the OPC UA server on the other end may send us (CVE-2022-25304).
 *
 * `transport_limits.py` is the Python half, and it does considerably more work:
 * python-opcua reassembles a chunked message into a list with nothing counting
 * it, so a server that streams Intermediate chunks and never terminates the
 * message exhausts the client — and that advisory has no fixed version and will
 * not get one, the library being unmaintained. The Python half therefore patches
 * the reassembly in addition to advertising the bounds.
 *
 * node-opcua needs none of that: it takes both bounds as transport settings and
 * enforces them in its own message builder. What matters here is that the numbers
 * are the *same* numbers — a limit one runtime applies and the other does not is a
 * difference in what the two are safe against — so they come from
 * `contract/tools.json` -> `transport` on both sides.
 */

import { CONTRACT } from "./contract.js";

export const MAX_CHUNK_COUNT = CONTRACT.transport.maxChunkCount;
export const MAX_CHUNK_SIZE = CONTRACT.transport.maxChunkSize;
export const MAX_MESSAGE_SIZE = CONTRACT.transport.maxMessageSize;

/** The transport settings to hand `OPCUAClient.create`.
 *
 * Stated rather than left to the library's defaults, for the same reason
 * `keepSessionAlive` is stated beside it: this is the behaviour the security
 * posture depends on, and a default can move in a future release.
 */
export function transportSettings(): {
  maxChunkCount: number;
  maxMessageSize: number;
  receiveBufferSize: number;
} {
  return {
    maxChunkCount: MAX_CHUNK_COUNT,
    maxMessageSize: MAX_MESSAGE_SIZE,
    receiveBufferSize: MAX_CHUNK_SIZE,
  };
}
