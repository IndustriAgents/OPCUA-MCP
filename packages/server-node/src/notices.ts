/** Messages a tool adds *beside* a successful result, from the contract.
 *
 * `notices.py` is the Python half. Separate from `errors.ts` because a notice is
 * not a failure: it rides along with a result that is otherwise complete, as a
 * trailing plain-text content block that is deliberately outside
 * `structuredContent` — a notice is not a record, and a client reading the
 * structured result must not have to filter it out.
 */

import { CONTRACT } from "./contract.js";

export const TEMPLATES: Record<string, string> = Object.fromEntries(
  Object.entries(CONTRACT.notices).filter(([key]) => !key.startsWith("$"))
) as Record<string, string>;

/** One contract notice with its placeholders filled in. */
export function notice(key: string, fields: Record<string, string | number> = {}): string {
  const template = TEMPLATES[key];
  if (template === undefined) {
    throw new Error(`No such contract notice template: ${key}`);
  }
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) => {
    const value = fields[name];
    return value === undefined ? placeholder : String(value);
  });
}
