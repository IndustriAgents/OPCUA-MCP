// One spelling of a node id, shared by everything that has to compare two.
//
// `node_ids.py` is the Python half and must agree case for case: the two
// runtimes read the *same* policy file, so a form one canonicalises and the
// other does not is an allowlist that authorises different writes depending on
// which server the operator happened to start.
//
// Two problems live here.
//
// **Spelling.** `i=2253` and `ns=0;i=2253` are the same node, and OPC UA lets
// either be written. Compared as raw strings they are not equal, so an
// allowlist entry in one form silently fails to match a request in the other —
// a *denial*, which is at least safe, but is indistinguishable from a policy
// mistake and sends people to the wrong place looking for it.
//
// **Namespace indexes are not stable.** `ns=2` means "the third entry of this
// server's NamespaceArray", which is assigned per session. A firmware update or
// a reordered namespace load can repoint it at a different URI, and an
// allowlist written as `ns=2;i=5` then authorises writes to a *different
// physical node* with nothing reporting anything wrong. The namespace URI is
// the stable name, so a policy may be written `nsu=<uri>;i=5` and is resolved
// against the live NamespaceArray each session.

/** The identifier forms OPC UA allows after the namespace part. */
const IDENTIFIER_PREFIXES = ["i=", "s=", "g=", "b="] as const;

/**
 * A node id in one canonical spelling.
 *
 * Trims, and writes a zero namespace that OPC UA allows to be left out.
 * Anything else — an `ns=N;` form, an `nsu=` form, an ExpandedNodeId carrying a
 * server index — is returned unchanged: those have no implicit part to restore,
 * and rewriting what is already explicit risks changing its meaning.
 */
export function canonicalNodeId(raw: string): string {
  const text = raw.trim();
  return IDENTIFIER_PREFIXES.some((prefix) => text.startsWith(prefix)) ? `ns=0;${text}` : text;
}

/** The `nsu=<uri>;<identifier>` form split into its parts, or null if not that form. */
export function namespaceUriForm(raw: string): { uri: string; identifier: string } | null {
  const text = raw.trim();
  if (!text.startsWith("nsu=")) return null;
  // The URI itself contains no `;` in practice, but an identifier may (`s=`
  // strings are arbitrary), so split on the *first* one only.
  const separator = text.indexOf(";");
  if (separator < 0) return null;
  const uri = text.slice("nsu=".length, separator);
  const identifier = text.slice(separator + 1);
  if (!uri || !identifier) return null;
  return { uri, identifier };
}

/**
 * A node id resolved against a server's NamespaceArray, canonically spelled.
 *
 * Returns null when an `nsu=` form names a URI this server does not publish —
 * which is a real answer, not a failure to compute one: the policy entry refers
 * to a namespace that is not here, so nothing can match it, and the caller must
 * deny rather than fall back to something that looks close.
 */
export function resolveNodeId(raw: string, namespaces: readonly string[]): string | null {
  const uriForm = namespaceUriForm(raw);
  if (!uriForm) return canonicalNodeId(raw);
  const index = namespaces.indexOf(uriForm.uri);
  return index < 0 ? null : `ns=${index};${uriForm.identifier}`;
}
