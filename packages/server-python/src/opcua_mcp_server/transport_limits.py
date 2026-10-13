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
2. :func:`install_receive_guard` checks the advertised chunk size before reading
   its body, and bounds both the chunk count and cumulative wire size during
   reassembly. Refused messages release their buffered chunks and fail the channel.

The patch is installed once over the library's receive paths. Tests exercise
actual wire headers and reassembly, including hostile headers whose bodies must
never be read. Node enforces the same contract bounds through node-opcua.

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

TOO_LARGE = (
    "OPC UA server exceeded {name} ({limit} bytes); refusing to buffer more "
    "(CVE-2022-25304). The channel will be rebuilt."
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
    """Bound inbound chunks before buffering and during reassembly. True if this call installed it.

    Idempotent, and safe to call before any connection exists. Installed at
    construction of the legacy rollback client, before any network request.
    Maintained clients use their own bounded protocol without patching this class.
    """
    global _installed
    with _install_lock:
        if _installed:
            return False
        from opcua import ua
        from opcua.common.connection import SecureConnection
        from opcua.ua.ua_binary import header_from_binary

        original = SecureConnection._receive
        original_socket = SecureConnection.receive_from_socket

        def size_of(chunk):
            # Real inbound chunks carry packet_size. The fallback also bounds
            # chunks constructed directly by an adapter or test.
            return chunk.MessageHeader.packet_size or len(chunk.Body) + 24

        def refuse(self, message):
            self._incoming_parts = []
            raise ua.UaError(message)

        def check(self, size, secure=True):
            if size > MAX_CHUNK_SIZE:
                refuse(self, TOO_LARGE.format(name="maxChunkSize", limit=MAX_CHUNK_SIZE))
            if secure:
                if chunk_count_exceeded(len(self._incoming_parts) + 1):
                    refuse(self, TOO_MANY_CHUNKS.format(limit=MAX_CHUNK_COUNT))
                if sum(size_of(part) for part in self._incoming_parts) + size > MAX_MESSAGE_SIZE:
                    refuse(self, TOO_LARGE.format(name="maxMessageSize", limit=MAX_MESSAGE_SIZE))

        def guarded(self, msg):
            check(self, size_of(msg))
            try:
                return original(self, msg)
            except Exception:
                self._incoming_parts = []
                raise

        def guarded_socket(self, socket):
            header = header_from_binary(socket)
            if header.body_size < 0:
                refuse(self, "Invalid OPC UA chunk size; the channel will be rebuilt.")
            check(
                self,
                header.packet_size,
                header.MessageType
                in (
                    ua.MessageType.SecureMessage,
                    ua.MessageType.SecureOpen,
                    ua.MessageType.SecureClose,
                ),
            )
            # No attacker-controlled body is read until all limits pass.
            body = socket.read(header.body_size)
            if len(body) != header.body_size:
                refuse(self, f"{header.body_size} bytes expected, {len(body)} available")
            return self.receive_from_header_and_body(header, ua.utils.Buffer(body))

        guarded.__wrapped__ = original
        guarded_socket.__wrapped__ = original_socket
        SecureConnection._receive = guarded
        SecureConnection.receive_from_socket = guarded_socket
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
