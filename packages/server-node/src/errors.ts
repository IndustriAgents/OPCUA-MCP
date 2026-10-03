/** The wording of every failure a tool call can return, from the contract.
 *
 * `errors.py` is the Python half, and the two are the same twenty lines on
 * purpose: the messages themselves live in `contract/tools.json` -> `errors`,
 * and each runtime only substitutes into them. They used to be hand-mirrored
 * string literals in `tools.ts` and `server.py`, which held for the message
 * *bodies* — someone checked — and quietly failed for the framing: this server
 * wrapped every failure as `Error: <message>` and the Python one did not, so the
 * same failure read two ways to a model and no test compared them.
 *
 * A tool failure is returned with `isError` set, which already says it is an
 * error, so neither runtime prefixes it now.
 */

import { CONTRACT } from "./contract.js";

/** The templates, straight from the contract. `$`-prefixed keys are prose for a
 * human reading the file, never a message. */
export const TEMPLATES: Record<string, string> = Object.fromEntries(
  Object.entries(CONTRACT.errors).filter(([key]) => !key.startsWith("$"))
) as Record<string, string>;

/** A refusal this server has already worded from the contract.
 *
 * Marked so the layers above can tell it from a failure that came back *from*
 * the OPC UA server. `write_opcua_nodes` wraps anything that goes wrong in
 * "Failed to write nodes: …", which is right for a status code the plant
 * returned and wrong for a value this server declined to send — that framing
 * says the plant rejected it, when the point of the refusal is that nothing
 * reached the plant at all. Python's `ToolError` plays the same part there.
 */
export class ContractRefusal extends Error {}

/** One contract error message with its placeholders filled in.
 *
 * An unknown key throws rather than returning something placeholder-shaped: a
 * message this server cannot word is a bug in this server, and finding it at the
 * first call beats shipping `{reason}` to an operator.
 */
export function message(key: string, fields: Record<string, string | number> = {}): string {
  return fill(TEMPLATES, "error", key, fields);
}

/** The other sentence tables, which are worded and filled exactly as `errors` is.
 *
 * Separate tables because they are separate kinds of answer — a skipped write is
 * one node's record, not a failure of the call; a write_access reason is a clause
 * inside a record; a policy-check finding is a line about the deployment, not
 * about a call — and keeping them apart is what stops one being used for another.
 */
const SECTIONS = {
  writeSkips: withoutProse(CONTRACT.writeSkips),
  writeAccessReasons: withoutProse(CONTRACT.writeAccessReasons),
  policyCheck: withoutProse(CONTRACT.policyCheck.messages),
};

/** One sentence from `writeSkips`, `writeAccessReasons` or `policyCheck.messages`. */
export function sentence(
  section: keyof typeof SECTIONS,
  key: string,
  fields: Record<string, string | number> = {}
): string {
  return fill(SECTIONS[section], section, key, fields);
}

function withoutProse(table: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(table).filter(([key]) => !key.startsWith("$")));
}

function fill(
  templates: Record<string, string>,
  kind: string,
  key: string,
  fields: Record<string, string | number>
): string {
  const template = templates[key];
  if (template === undefined) {
    throw new Error(`No such contract ${kind} template: ${key}`);
  }
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) => {
    const value = fields[name];
    return value === undefined ? placeholder : String(value);
  });
}
