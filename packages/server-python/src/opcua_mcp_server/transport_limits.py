"""Bound what the OPC UA server on the other end may send us (CVE-2022-25304).

``transport-limits.ts`` is the Node half. The bounds themselves live in
``contract/tools.json`` -> ``transport``, because a limit one runtime enforces and
the other does not is a difference in what the two are safe against.

**The vulnerability.** An OPC UA message may be split across chunks, and
``python-opcua`` reassembles them like this
(``opcua/common/connection.py``, ``SecureConnection._receive``):

    self._incoming_parts.append(msg)
    if msg.MessageHeader.ChunkType == ua.ChunkType.Intermediate:
        return None

Nothing counts them. A server that sends Intermediate chunks and never sends the
Final one grows that list until the process runs out of memory. CVE-2022-25304
has no fixed version and is not going to get one: ``python-opcua`` is
unmaintained, and the advisory names ``asyncua`` too.

**What this module does about it**, in two parts, because the first alone is not
protection:

1. :func:`advertise_limits` sets the ``MaxChunkCount`` and ``MaxMessageSize``
   this client asks for in the OPC UA Hello. A conforming server then stays
   inside them. That is protocol hygiene, and it binds only a server that
   chooses to obey — which a hostile one does not.
2. :func:`install_receive_guard` is the enforcement: it wraps
   ``SecureConnection._receive`` so a message that exceeds ``maxChunkCount``
   chunks raises instead of being accumulated. The channel then fails the way any
   other protocol violation does, and the connection layer reconnects.

**On patching a dependency.** This reaches into a third-party class, which is
worth being uneasy about. It is done because the alternative is shipping a client
that a compromised PLC can exhaust, against a library that will not be fixed: the
patch is one wrapper, applied once, over a method whose whole body is quoted
above, and ``tests/unit/test_transport_limits.py`` drives real chunk sequences
through it so a change in the library that moves the ground under it fails a test
rather than silently disabling the guard. The Node runtime needs none of this —
``node-opcua`` takes the same two bounds as transport settings and enforces them
itself.
"""

from __future__ import annotations

import threading
from typing import Any

from .contract import CONTRACT

TRANSPORT = CONTRACT["transport"]
MAX_CHUNK_COUNT: int = TRANSPORT["maxChunkCount"]
MAX_CHUNK_SIZE: int = TRANSPORT["maxChunkSize"]
MAX_MESSAGE_SIZE: int = TRANSPORT["maxMessageSize"]

#: Worded here rather than in ``contract.errors``: this never reaches a model as
#: a tool result. It fails the secure channel, and the connection layer treats it
#: as a dead session — so its audience is an operator reading stderr.
TOO_MANY_CHUNKS = (
    "OPC UA server sent more than {limit} chunks in one message without "
    "terminating it; refusing to buffer more (CVE-2022-25304). The channel will "
    "be rebuilt."
)

_installed = False
_install_lock = threading.Lock()


def advertise_limits(client: Any) -> None:
    """Ask the server to stay inside our bounds, in the Hello.

    ``python-opcua`` defaults both to ``0``, which means "no limit" — it is the
    client telling the server it will accept anything.
    """
    client.max_chunkcount = MAX_CHUNK_COUNT
    client.max_messagesize = MAX_MESSAGE_SIZE


def chunk_count_exceeded(parts: int) -> bool:
    """Whether accumulating one more chunk would pass the cap.

    Free of any OPC UA type so the unit suite can pin the boundary exactly:
    ``MAX_CHUNK_COUNT`` chunks is a legal message, one more is not.
    """
    return parts > MAX_CHUNK_COUNT


def install_receive_guard() -> bool:
    """Bound ``SecureConnection._incoming_parts``. True if this call installed it.

    Idempotent, and safe to call before any connection exists. Installed at
    import of the connection layer rather than per client, because the list being
    bounded belongs to the library's class, not to ours.
    """
    global _installed
    with _install_lock:
        if _installed:
            return False
        from opcua.common.connection import SecureConnection

        original = SecureConnection._receive

        def guarded(self, msg):
            # Counted *before* the library appends, so the cap is the number of
            # chunks held rather than one more than it.
            if chunk_count_exceeded(len(self._incoming_parts) + 1):
                # Drop what was accumulated: the message is being refused, and
                # keeping it would leak the memory this exists to protect.
                self._incoming_parts = []
                from opcua import ua

                raise ua.UaError(TOO_MANY_CHUNKS.format(limit=MAX_CHUNK_COUNT))
            return original(self, msg)

        guarded.__wrapped__ = original
        SecureConnection._receive = guarded
        _installed = True
        return True


__all__ = [
    "MAX_CHUNK_COUNT",
    "MAX_CHUNK_SIZE",
    "MAX_MESSAGE_SIZE",
    "TOO_MANY_CHUNKS",
    "advertise_limits",
    "chunk_count_exceeded",
    "install_receive_guard",
]
