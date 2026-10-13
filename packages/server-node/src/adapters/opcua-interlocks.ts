/** node-opcua reads behind the interlock port: one chunked Read of the requirement nodes. */
import { AttributeIds, type ClientSession, type DataValue } from "node-opcua-client";
import { isConnectionError } from "../connection.js";
import { AdapterFailure, describeError } from "../errors.js";
import { chunked } from "../limits.js";
import type { RequirementReading } from "../policy-check.js";
import { variantToJson } from "../records.js";
import { isGood } from "../status.js";

/** Each requirement node's Value as the interlock check reads it.
 *
 * A Bad value is a reading with that status, which does not hold. A failure of
 * the read itself is every node's reading — unless it is the connection, which
 * propagates, as the write or call it precedes would have met it too.
 */
export async function readRequirements(
  session: ClientSession,
  nodeIds: string[],
  chunk: number
): Promise<RequirementReading[]> {
  try {
    const values: DataValue[] = [];
    for (const part of chunked(nodeIds, chunk)) {
      values.push(
        ...(await session.read(part.map((nodeId) => ({ nodeId, attributeId: AttributeIds.Value }))))
      );
    }
    return nodeIds.map((_, index) => {
      const dataValue = values[index];
      return dataValue && isGood(dataValue.statusCode)
        ? { status: "Good", value: variantToJson(dataValue.value) }
        : { status: dataValue?.statusCode?.name ?? "BadUnexpectedError" };
    });
  } catch (error) {
    if (isConnectionError(error)) {
      throw new AdapterFailure("interlocks", describeError(error), error);
    }
    return nodeIds.map(() => ({ status: describeError(error) }));
  }
}
