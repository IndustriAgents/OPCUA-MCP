/** A valid UTF-16 pair is one scalar; only isolated surrogates cannot encode as UTF-8. */
export function hasUnpairedSurrogate(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0)!;
    if (code >= 0xd800 && code <= 0xdfff) return true;
  }
  return false;
}
