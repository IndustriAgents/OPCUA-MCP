// A small in-memory OPC UA address space that answers the services the
// information-model checks use: Read, Browse (forward hierarchical, forward
// all, inverse), BrowseNext and TranslateBrowsePathsToNodeIds.
//
// Not a test file itself — `node-facts.test.mjs` and `policy-resolution.test.mjs`
// share it. It answers the way a server does, not the way node-opcua's types
// say: a NodeId is anything with `toString()`, a status anything with `name`,
// which is all the code under test reads.
import { AttributeIds, NodeClass, StatusCodes } from "node-opcua-client";

import { canonicalNodeId } from "../build/node-ids.js";

const HIERARCHICAL = 35;
const HAS_TYPE_DEFINITION = 40;
const HAS_SUBTYPE = 45;

const id = (text) => ({ toString: () => text });

/** `nodes` maps a node id to its definition:
 *
 * - `class`: a NodeClass name ("Object", "Variable", …)
 * - `name`: its BrowseName as `ns:Name`
 * - `parent`: the node it hangs off, hierarchically
 * - `type`: its HasTypeDefinition target
 * - `supertype`: for a type, its HasSubtype parent
 * - `attributes`: attribute name → value ("DataType", "ValueRank", "Value", …),
 *   or a status name to answer Bad with, as `{ bad: "BadNotReadable" }`
 * - `browseStatus`: a status name its own browse answers with instead
 *
 * Every call is recorded in `calls`, so a test can count round trips.
 */
export function fakeAddressSpace(nodes, { failBrowseOf = new Set() } = {}) {
  const calls = { read: [], browse: [], translate: [], browseNext: 0 };
  const children = new Map();
  for (const [nodeId, node] of Object.entries(nodes)) {
    if (!node.parent) continue;
    if (!children.has(node.parent)) children.set(node.parent, []);
    children.get(node.parent).push(nodeId);
  }

  const reference = (target, referenceType) => {
    const node = nodes[target] ?? {};
    const [namespaceIndex, name] = (node.name ?? "0:?").split(/:(.*)/s);
    return {
      nodeId: id(target),
      browseName: { namespaceIndex: Number(namespaceIndex), name },
      nodeClass: NodeClass[node.class ?? "Unspecified"],
      // What python-opcua reports for a retyped node: the type it was created
      // with, not the one it has. The code under test must not rely on it.
      typeDefinition: id("ns=0;i=63"),
      referenceTypeId: { namespace: 0, value: referenceType },
    };
  };

  const browseOne = (description) => {
    const nodeId = canonicalNodeId(String(description.nodeId));
    const node = nodes[nodeId];
    if (failBrowseOf.has(nodeId)) throw new Error(`browse of ${nodeId} failed`);
    if (!node) return { statusCode: StatusCodes.BadNodeIdUnknown, references: [] };
    if (node.browseStatus) {
      return { statusCode: StatusCodes[node.browseStatus], references: [] };
    }
    let references;
    if (description.browseDirection === 1 /* Inverse */) {
      references = node.supertype ? [reference(node.supertype, HAS_SUBTYPE)] : [];
    } else if (String(description.referenceTypeId) === "ns=0;i=40") {
      references = node.type ? [reference(node.type, HAS_TYPE_DEFINITION)] : [];
    } else {
      references = (children.get(nodeId) ?? []).map((child) => reference(child, HIERARCHICAL));
      if (!description.referenceTypeId && node.type) {
        references.push(reference(node.type, HAS_TYPE_DEFINITION));
      }
    }
    return { statusCode: StatusCodes.Good, references };
  };

  const attribute = (nodeId, attributeId) => {
    const node = nodes[canonicalNodeId(String(nodeId))];
    if (!node) return { statusCode: StatusCodes.BadNodeIdUnknown, value: { value: null } };
    const name = AttributeIds[attributeId];
    if (name === "NodeClass") {
      return { statusCode: StatusCodes.Good, value: { value: NodeClass[node.class] } };
    }
    const value = node.attributes?.[name];
    if (value === undefined) {
      return { statusCode: StatusCodes.BadAttributeIdInvalid, value: { value: null } };
    }
    if (value && typeof value === "object" && "bad" in value) {
      return { statusCode: StatusCodes[value.bad], value: { value: null } };
    }
    return {
      statusCode: StatusCodes.Good,
      value: { value: name === "DataType" ? id(value) : value },
    };
  };

  return {
    calls,
    async read(items) {
      const list = Array.isArray(items) ? items : [items];
      calls.read.push(list.length);
      const values = list.map((item) => attribute(item.nodeId, item.attributeId));
      return Array.isArray(items) ? values : values[0];
    },
    async browse(descriptions) {
      const list = Array.isArray(descriptions) ? descriptions : [descriptions];
      calls.browse.push(list.length);
      const results = list.map(browseOne);
      return Array.isArray(descriptions) ? results : results[0];
    },
    async browseNext() {
      calls.browseNext += 1;
      return { statusCode: StatusCodes.Good, references: [] };
    },
    async translateBrowsePath(paths) {
      const list = Array.isArray(paths) ? paths : [paths];
      calls.translate.push(list.length);
      const results = list.map((path) => {
        const start = canonicalNodeId(path.startingNode.toString());
        const [element] = path.relativePath.elements;
        const wanted = `${element.targetName.namespaceIndex}:${element.targetName.name}`;
        const found = (children.get(start) ?? []).find((child) => nodes[child].name === wanted);
        return found
          ? { statusCode: StatusCodes.Good, targets: [{ targetId: id(found) }] }
          : { statusCode: StatusCodes.BadNoMatch, targets: [] };
      });
      return Array.isArray(paths) ? results : results[0];
    },
  };
}

/** A LocalizedText as node-opcua decodes one. */
export function text(value) {
  return { text: value, locale: null };
}
