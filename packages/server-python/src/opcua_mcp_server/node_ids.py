"""One spelling of a node id, shared by everything that has to compare two.

``node-ids.ts`` is the Node half and must agree case for case: the two runtimes
read the *same* policy file, so a form one canonicalises and the other does not
is an allowlist that authorises different writes depending on which server the
operator happened to start.

Two problems live here.

**Spelling.** ``i=2253`` and ``ns=0;i=2253`` are the same node, and OPC UA lets
either be written. Compared as raw strings they are not equal, so an allowlist
entry in one form silently fails to match a request in the other — a *denial*,
which is at least safe, but is indistinguishable from a policy mistake and sends
people to the wrong place looking for it.

**Namespace indexes are not stable.** ``ns=2`` means "the third entry of this
server's NamespaceArray", which is assigned per session. A firmware update or a
reordered namespace load can repoint it at a different URI, and an allowlist
written as ``ns=2;i=5`` then authorises writes to a *different physical node*
with nothing reporting anything wrong. The namespace URI is the stable name, so
a policy may be written ``nsu=<uri>;i=5`` and is resolved against the live
NamespaceArray each session.
"""

from __future__ import annotations

from collections.abc import Sequence

#: The identifier forms OPC UA allows after the namespace part.
IDENTIFIER_PREFIXES = ("i=", "s=", "g=", "b=")


def canonical_node_id(raw: str) -> str:
    """A node id in one canonical spelling.

    Trims, and writes a zero namespace that OPC UA allows to be left out.
    Anything else — an ``ns=N;`` form, an ``nsu=`` form, an ExpandedNodeId
    carrying a server index — is returned unchanged: those have no implicit part
    to restore, and rewriting what is already explicit risks changing its
    meaning.
    """
    text = raw.strip()
    return f"ns=0;{text}" if text.startswith(IDENTIFIER_PREFIXES) else text


def namespace_uri_form(raw: str) -> tuple[str, str] | None:
    """The ``nsu=<uri>;<identifier>`` form split into its parts, or None."""
    text = raw.strip()
    if not text.startswith("nsu="):
        return None
    # The URI itself contains no ";" in practice, but an identifier may ("s="
    # strings are arbitrary), so split on the *first* one only.
    uri, separator, identifier = text[len("nsu=") :].partition(";")
    if not separator or not uri or not identifier:
        return None
    return uri, identifier


def resolve_node_id(raw: str, namespaces: Sequence[str]) -> str | None:
    """A node id resolved against a server's NamespaceArray, canonically spelled.

    Returns None when an ``nsu=`` form names a URI this server does not publish
    — which is a real answer, not a failure to compute one: the policy entry
    refers to a namespace that is not here, so nothing can match it, and the
    caller must deny rather than fall back to something that looks close.
    """
    uri_form = namespace_uri_form(raw)
    if uri_form is None:
        return canonical_node_id(raw)
    uri, identifier = uri_form
    try:
        index = list(namespaces).index(uri)
    except ValueError:
        return None
    return f"ns={index};{identifier}"
