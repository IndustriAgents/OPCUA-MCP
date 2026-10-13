"""Exercise native asyncua framing while retaining independent local caps."""

from __future__ import annotations

import asyncio
import struct

import pytest
from asyncua import ua
from asyncua.common.connection import MessageChunk, TransportLimits
from asyncua.ua.ua_binary import header_from_binary, struct_from_binary, uatcp_to_binary
from opcua_mcp_server.adapters.asyncua_transport import BoundedProtocol, contract_limits


class Transport:
    def __init__(self):
        self.closed = False
        self.writes = []

    def close(self):
        self.closed = True

    def write(self, data):
        self.writes.append(data)


def protocol(**caps):
    limits = contract_limits()
    for name, value in caps.items():
        setattr(limits, name, value)
    instance = BoundedProtocol(limits=limits)
    transport = Transport()
    instance.connection_made(transport)
    return instance, transport


def secure_chunk(instance, kind=ua.ChunkType.Intermediate, sequence=1, payload=b"body"):
    connection = instance._connection
    chunk = MessageChunk(
        connection.security_policy.symmetric_cryptography, payload, chunk_type=kind
    )
    chunk.MessageHeader.ChannelId = connection.security_token.ChannelId
    chunk.SecurityHeader.TokenId = connection.security_token.TokenId
    chunk.SequenceHeader.RequestId = 1
    chunk.SequenceHeader.SequenceNumber = sequence
    return chunk.to_binary()


@pytest.mark.parametrize("kind", [b"MSG", b"OPN", b"CLO", b"ACK", b"ERR"])
@pytest.mark.parametrize("split", [1, 4, 7, 8])
def test_hostile_header_refuses_before_waiting_for_body(kind, split):
    instance, transport = protocol()
    header = struct.pack("<3scI", kind, b"F", 2**31)
    instance.data_received(header[:split])
    if split < 8:
        assert not transport.closed
    instance.data_received(header[split:])
    assert transport.closed
    assert instance._pending_chunk == b""
    assert instance.receive_buffer is None
    assert instance._connection._incoming_parts == []
    instance.data_received(b"ignored after refusal")
    assert instance._pending_chunk == b""


@pytest.mark.parametrize("size", [0, 7, 8, 23])
def test_invalid_secure_chunk_size_fails_the_channel(size):
    instance, transport = protocol()
    instance.data_received(struct.pack("<3scI", b"MSG", b"F", size))
    assert transport.closed
    assert not instance._pending_chunk


def test_local_caps_cannot_be_disabled_by_injected_unlimited_limits():
    instance = BoundedProtocol(limits=TransportLimits(2**31, 2**31, 0, 0))
    assert instance._connection._limits == contract_limits()


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", [0, 2**31 - 1])
async def test_ack_cannot_widen_local_limits(peer):
    instance, transport = protocol()
    initial = contract_limits()
    future = asyncio.get_running_loop().create_future()
    instance._callbackmap[0] = future
    ack = ua.Acknowledge(
        ReceiveBufferSize=peer, SendBufferSize=peer, MaxMessageSize=peer, MaxChunkCount=peer
    )
    wire = uatcp_to_binary(ua.MessageType.Acknowledge, ack)
    for byte in wire:
        instance.data_received(bytes([byte]))
    assert await future == ack
    assert not transport.closed
    assert instance._connection._limits == initial


@pytest.mark.asyncio
async def test_ack_narrows_correct_buffer_direction_and_never_reopens_it():
    instance, _ = protocol()
    for ack in [
        ua.Acknowledge(
            ReceiveBufferSize=8192, SendBufferSize=16384, MaxMessageSize=32768, MaxChunkCount=2
        ),
        ua.Acknowledge(),
    ]:
        future = asyncio.get_running_loop().create_future()
        instance._callbackmap[0] = future
        instance.data_received(uatcp_to_binary(ua.MessageType.Acknowledge, ack))
        await future
    caps = instance._connection._limits
    assert (
        caps.max_send_buffer,
        caps.max_recv_buffer,
        caps.max_message_size,
        caps.max_chunk_count,
    ) == (8192, 16384, 32768, 2)


@pytest.mark.asyncio
async def test_real_reassembly_at_message_boundary_releases_then_accepts_next_message():
    instance, transport = protocol(max_message_size=56)
    for request, first_sequence in [(1, 1), (1, 3)]:
        future = asyncio.get_running_loop().create_future()
        instance._callbackmap[request] = future
        first = secure_chunk(instance, sequence=first_sequence)
        final = secure_chunk(instance, ua.ChunkType.Single, sequence=first_sequence + 1)
        assert len(first) + len(final) == 56
        # Multiple chunks in one data callback must remain valid.
        instance.data_received(first + final)
        result = await asyncio.wait_for(future, 1)
        assert bytes(result) == b"bodybody"
        assert not instance._connection._incoming_parts
        assert not transport.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("caps", [{"max_message_size": 55}, {"max_chunk_count": 1}])
async def test_message_or_chunk_count_excess_fails_pending_requests_and_releases_memory(caps):
    instance, transport = protocol(**caps)
    future = asyncio.get_running_loop().create_future()
    instance._callbackmap[1] = future
    instance.data_received(secure_chunk(instance))
    assert len(instance._connection._incoming_parts) == 1
    # The second chunk's body is deliberately absent.
    instance.data_received(secure_chunk(instance, sequence=2)[:8])
    with pytest.raises(ua.UaStatusCodeError) as raised:
        await future
    assert raised.value.code == ua.StatusCodes.BadRequestTooLarge
    assert transport.closed
    assert not instance._pending_chunk
    assert not instance._connection._incoming_parts


@pytest.mark.asyncio
async def test_hello_advertises_the_actual_receive_bounds_and_cleans_callback():
    instance, transport = protocol()
    task = asyncio.create_task(instance.send_hello("opc.tcp://localhost:4840"))
    await asyncio.sleep(0)
    buffer = ua.utils.Buffer(transport.writes[0])
    header = header_from_binary(buffer)
    assert header.MessageType == ua.MessageType.Hello
    hello = struct_from_binary(ua.Hello, buffer)
    caps = contract_limits()
    assert (
        hello.ReceiveBufferSize,
        hello.SendBufferSize,
        hello.MaxMessageSize,
        hello.MaxChunkCount,
    ) == (caps.max_recv_buffer, caps.max_send_buffer, caps.max_message_size, caps.max_chunk_count)
    instance.data_received(uatcp_to_binary(ua.MessageType.Acknowledge, ua.Acknowledge()))
    await task
    assert 0 not in instance._callbackmap


@pytest.mark.asyncio
async def test_hello_timeout_cleans_its_callback():
    instance, _ = protocol()
    instance.timeout = 0.001
    with pytest.raises(asyncio.TimeoutError):
        await instance.send_hello("opc.tcp://localhost:4840")
    assert 0 not in instance._callbackmap
