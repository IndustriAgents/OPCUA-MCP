// Browsing the address space, with continuation points drained.
//
// The Python server's `browse_children` (server.py) is the other half of this:
// both must answer the same question with the same completeness, because a
// browse that stops early is not an error anyone sees — it is a *shorter list*,
// which reads exactly like a node that really does have fewer children.
import {
  BrowseDirection,
  BrowseResult,
  ClientSession,
  ReferenceDescription,
  StatusCodes,
} from "node-opcua-client";

/** The browse this server performs: hierarchical references, forward only.
 *
 * Spelled out rather than relying on `session.browse(nodeId)`'s string form,
 * which happens to coerce to the same thing today. The Python server has to be
 * explicit here — `python-opcua`'s `get_references()` defaults to *all*
 * reference types in *both* directions, which walks back up to the parent and
 * out to the type definition — so stating it on both sides keeps the two
 * implementations comparable by reading them, not by knowing each library's
 * defaults.
 */
function browseDescription(nodeId: string) {
  return {
    nodeId,
    browseDirection: BrowseDirection.Forward,
    referenceTypeId: "HierarchicalReferences",
    includeSubtypes: true,
    nodeClassMask: 0,
    resultMask: 63,
  };
}

/**
 * Every forward hierarchical reference of a node, following continuation points.
 *
 * A server may cap how many references one response carries whatever the client
 * asks for, and answers the rest behind a continuation point. Taking only the
 * first `BrowseResult` — which is what this server did until now — silently
 * returns a truncated child list as a success. On a plant server with a wide
 * node, that is a wrong answer delivered confidently, which is the worst kind.
 *
 * Every result is status-checked, the continued ones included: a server that
 * expires or refuses a continuation point answers with a bad status and no
 * references, which unchecked would end the loop and truncate just the same.
 *
 * Note for anyone looking for an end-to-end test of this: the bundled mock
 * (`python-opcua`'s server) never issues a continuation point — it has no
 * server-side implementation of them and ignores `RequestedMaxReferencesPerNode`
 * — so no mock can exercise the loop. It is pinned by unit tests that stub the
 * session instead (`test/unit.test.mjs`).
 */
export async function browseAllReferences(
  session: ClientSession,
  nodeId: string
): Promise<ReferenceDescription[]> {
  const references: ReferenceDescription[] = [];
  let result: BrowseResult = await session.browse(browseDescription(nodeId));

  for (;;) {
    if (result.statusCode !== StatusCodes.Good) {
      // `.name`, not the whole StatusCode: node-opcua renders the same rejection
      // as `BadNodeIdUnknown (0x80340000)` and python-opcua as
      // `StatusCode(BadNodeIdUnknown)`. Neither server controls the other's
      // spelling, but both can name the status plainly — and then the two
      // runtimes fail with the same sentence.
      throw new Error(`Browse failed with status: ${result.statusCode.name}`);
    }

    references.push(...(result.references ?? []));

    const continuationPoint = result.continuationPoint;
    if (!continuationPoint || continuationPoint.length === 0) {
      return references;
    }

    // `false` — do not release. Releasing a continuation point tells the server
    // to discard the rest of the answer; this asks for it.
    result = await session.browseNext(continuationPoint, false);
  }
}

/** Which of a node's HasTypeDefinition references to report, if any.
 *
 * Split out from the browse and driven by `tests/fixtures/type-definitions.json`
 * because `tests/unit/test_type_definitions.py` has to answer identically: two
 * clients browsing the same server must not disagree about what its nodes are.
 *
 * Exactly one, or nothing. OPC UA Part 3 §4.3 gives an Object or a Variable
 * exactly one HasTypeDefinition, so:
 *
 * - none — a Method, a View or a type itself. That is an answer, not a failure.
 * - two — a server no client can read correctly. Taking whichever came first
 *   would let the two runtimes report different types for the same node
 *   depending on how each library ordered the references, and would report the
 *   *base* type for a node that also declared a useful one. Saying nothing is
 *   the only answer that is both deterministic and never wrong.
 */
export function typeDefinitionOf(isGood: boolean, browseNames: string[]): string | null {
  if (!isGood || browseNames.length !== 1) return null;
  return browseNames[0] || null;
}
