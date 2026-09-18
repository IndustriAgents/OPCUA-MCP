// What a tool's `accessClass` may say, as a runtime list and as a type.
//
// Its own module rather than part of `contract.ts` for two reasons. It is not
// contract *data* — it is the set of values the contract's `accessClass` field
// is allowed to take, which is a property of this code, not of the file. And
// `contract.ts` is replaced wholesale by a generated stub when the single-file
// and .mcpb artifacts are bundled, so a runtime value added there has to be
// repeated in `scripts/bundle.mjs` or the bundle fails to build. It was, and it
// did; the artifact smoke tests caught it. Nothing here is stubbed.
//
// `ACCESS_CLASSES` in `policy.py` is the Python half of this.

/** Every access class the contract may declare.
 *
 * A list and not only a type, because the policy layer has to recognise an
 * *unknown* class at runtime: a contract is data, and data can carry a typo that
 * the compiler never sees.
 */
export const ACCESS_CLASSES = ["read", "monitor", "alarm-action", "control"] as const;

export type AccessClass = (typeof ACCESS_CLASSES)[number];
