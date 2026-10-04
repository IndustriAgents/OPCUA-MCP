"""Instance-scoped receive limits for asyncua 2.0, before body buffering.

The maintained library may widen client receive limits from an Ack and does not
bound cumulative reassembly bytes. This adapter retains local caps regardless
of peer advertisements. It does not patch process-global library classes.
"""

from __future__ import annotations

import asyncio
import struct

from asyncua import ua
from asyncua.client.ua_client import UASocketProtocol
from asyncua.common.connection import SecureConnection, TransportLimits
from asyncua.ua.ua_binary import uatcp_to_binary

from ..contract import CONTRACT


class BoundedLimits(TransportLimits):
    """An acknowledgement can only narrow local receive and send limits."""

    def update_client_limits(self, msg: ua.Acknowledge) -> None:
        def narrow(local: int, peer: int) -> int:
            return min(local, peer) if peer else local

        # Part 6: peer SendBufferSize limits what this client receives.
        self.max_recv_buffer = narrow(self.max_recv_buffer, msg.SendBufferSize)
        self.max_send_buffer = narrow(self.max_send_buffer, msg.ReceiveBufferSize)
        self.max_message_size = narrow(self.max_message_size, msg.MaxMessageSize)
        self.max_chunk_count = narrow(self.max_chunk_count, msg.MaxChunkCount)


def contract_limits() -> BoundedLimits:
    caps = CONTRACT["transport"]
    return BoundedLimits(
        max_recv_buffer=caps["maxChunkSize"],
        max_send_buffer=caps["maxChunkSize"],
        max_message_size=caps["maxMessageSize"],
        max_chunk_count=caps["maxChunkCount"],
    )


class BoundedConnection(SecureConnection):
    def check_header(self, kind: bytes, size: int) -> None:
        caps = self._limits
        if size < 8 or size > caps.max_recv_buffer:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadRequestTooLarge)
        if kind in (
            ua.MessageType.SecureMessage,
            ua.MessageType.SecureOpen,
            ua.MessageType.SecureClose,
        ):
            if size < 24:
                raise ua.UaStatusCodeError(ua.StatusCodes.BadRequestTooLarge)
            total = sum(part.MessageHeader.packet_size for part in self._incoming_parts)
            if (
                len(self._incoming_parts) + 1 > caps.max_chunk_count
                or total + size > caps.max_message_size
            ):
                raise ua.UaStatusCodeError(ua.StatusCodes.BadRequestTooLarge)

    def receive_from_header_and_body(self, header, body):
        try:
            self.check_header(header.MessageType, header.packet_size)
            return super().receive_from_header_and_body(header, body)
        except Exception:
            self._incoming_parts = []
            raise


class BoundedProtocol(UASocketProtocol):
    """Feed native parsing one bounded chunk at a time, including split headers."""

    def __init__(self, timeout=4, security_policy=None, limits=None):
        caps = contract_limits()
        if limits is not None:
            for name in (
                "max_recv_buffer",
                "max_send_buffer",
                "max_message_size",
                "max_chunk_count",
            ):
                configured = getattr(limits, name)
                if configured > 0:
                    setattr(caps, name, min(getattr(caps, name), configured))
        if security_policy is None:
            super().__init__(timeout, limits=caps)
        else:
            super().__init__(timeout, security_policy=security_policy, limits=caps)
        self._connection = BoundedConnection(
            self._connection.security_policy, self._connection._limits
        )
        self._pending_chunk = bytearray()
        self._refused = False

    def _refuse(self, error):
        self._refused = True
        self._pending_chunk.clear()
        self.receive_buffer = None
        self._connection._incoming_parts = []
        self._fail_all_pending(error)
        self.disconnect_socket()

    def data_received(self, data: bytes) -> None:
        if self._refused:
            return
        remaining = memoryview(data)
        try:
            while remaining:
                if len(self._pending_chunk) < 8:
                    take = min(8 - len(self._pending_chunk), len(remaining))
                    self._pending_chunk.extend(remaining[:take])
                    remaining = remaining[take:]
                    if len(self._pending_chunk) < 8:
                        return
                kind, _, size = struct.unpack("<3scI", self._pending_chunk[:8])
                self._connection.check_header(kind, size)
                take = min(size - len(self._pending_chunk), len(remaining))
                self._pending_chunk.extend(remaining[:take])
                remaining = remaining[take:]
                if len(self._pending_chunk) < size:
                    return
                chunk = bytes(self._pending_chunk)
                self._pending_chunk.clear()
                super().data_received(chunk)
                if self.transport is None:
                    return
        except ua.UaStatusCodeError as error:
            self._refuse(error)

    async def send_hello(self, url, max_messagesize=0, max_chunkcount=0):
        caps = self._connection._limits
        hello = ua.Hello(
            EndpointUrl=url,
            ReceiveBufferSize=caps.max_recv_buffer,
            SendBufferSize=caps.max_send_buffer,
            MaxMessageSize=caps.max_message_size,
            MaxChunkCount=caps.max_chunk_count,
        )
        ack = asyncio.get_running_loop().create_future()
        self._callbackmap[0] = ack
        try:
            if self.transport is None:
                raise ConnectionError("OPC UA transport is closed")
            self.transport.write(uatcp_to_binary(ua.MessageType.Hello, hello))
            return await asyncio.wait_for(ack, self.timeout)
        finally:
            self._callbackmap.pop(0, None)
