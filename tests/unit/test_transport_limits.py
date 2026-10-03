"""The guard against CVE-2022-25304, driven through python-opcua's own reassembly.

The vulnerability is a missing bound: ``SecureConnection._receive`` appends every
Intermediate chunk to ``self._incoming_parts`` and nothing counts them, so a
server that never terminates the message grows that list until the process is out
of memory. There is no fixed version and there will not be one — python-opcua is
unmaintained, and the advisory names asyncua too.

So these tests drive the *real* class rather than a stand-in. A patch over a
third-party method is only as good as the assumption that the method still looks
the way it did, and that assumption is what is being checked here: if a future
python-opcua moves the reassembly, or renames ``_incoming_parts``, or stops
raising ``UaError``, one of these fails instead of the guard silently doing
nothing.
"""

from __future__ import annotations

import json
import struct

import opcua_mcp_server.transport_limits as limits
import pytest
from conftest import ROOT
from opcua import ua
from opcua.common.connection import MessageChunk, SecureConnection
from opcua_mcp_server.transport_limits import (
    MAX_CHUNK_COUNT,
    MAX_CHUNK_SIZE,
    MAX_MESSAGE_SIZE,
    advertise_limits,
    chunk_count_exceeded,
    install_receive_guard,
)

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def guard_installed():
    """The guard, however this module happens to be imported.

    ``install_receive_guard`` is idempotent and is normally installed by importing
    the connection layer; calling it here means these tests do not depend on
    another module having been imported first.
    """
    install_receive_guard()


def _chunk(chunk_type, request_id: int = 1, sequence: int = 1) -> MessageChunk:
    """One message chunk, as python-opcua's own reassembly builds them."""
    chunk = MessageChunk(None, b"body", ua.MessageType.SecureMessage, chunk_type)
    chunk.SequenceHeader.RequestId = request_id
    chunk.SequenceHeader.SequenceNumber = sequence
    return chunk


def _connection() -> SecureConnection:
    connection = SecureConnection(ua.SecurityPolicy())
    # A fresh channel has no peer sequence number yet, and the sequence checks are
    # not what is under test here.
    connection._peer_sequence_number = None
    return connection


def test_the_numbers_come_from_the_contract():
    transport = CONTRACT["transport"]
    assert transport["maxChunkCount"] == MAX_CHUNK_COUNT
    assert transport["maxChunkSize"] == MAX_CHUNK_SIZE
    assert transport["maxMessageSize"] == MAX_MESSAGE_SIZE
    # The message bound has to be the one the chunk bounds imply, or the two
    # limits contradict each other and the looser one is the real one.
    assert MAX_CHUNK_COUNT * MAX_CHUNK_SIZE == MAX_MESSAGE_SIZE


@pytest.mark.parametrize(
    ("parts", "exceeded"),
    [
        (1, False),
        (MAX_CHUNK_COUNT - 1, False),
        (MAX_CHUNK_COUNT, False),
        (MAX_CHUNK_COUNT + 1, True),
    ],
)
def test_the_boundary_is_where_the_contract_puts_it(parts, exceeded):
    """``maxChunkCount`` chunks is a legal message; one more is not."""
    assert chunk_count_exceeded(parts) is exceeded


def test_a_normal_multi_chunk_message_still_reassembles():
    """The guard must not break the thing it is guarding.

    A large-but-legitimate OPC UA response really does arrive in many chunks —
    a browse of a big folder, or a history read — so refusing those would trade a
    denial of service for a denial of service.
    """
    connection = _connection()
    for sequence in range(1, 51):
        assert connection._receive(_chunk(ua.ChunkType.Intermediate, sequence=sequence)) is None
    message = connection._receive(_chunk(ua.ChunkType.Single, sequence=51))
    assert message is not None, "a terminated message must still be assembled"
    assert connection._incoming_parts == [], "a delivered message must not be left buffered"


def test_a_server_that_never_terminates_the_message_is_cut_off():
    """The attack, as CVE-2022-25304 describes it."""
    connection = _connection()
    with pytest.raises(ua.UaError) as raised:
        for sequence in range(1, MAX_CHUNK_COUNT + 5):
            connection._receive(_chunk(ua.ChunkType.Intermediate, sequence=sequence))

    assert "CVE-2022-25304" in str(raised.value), str(raised.value)
    assert str(MAX_CHUNK_COUNT) in str(raised.value)
    # And the memory is released rather than held by the refused message: a guard
    # that raises but keeps the buffer leaks exactly what it exists to protect.
    assert connection._incoming_parts == []


def test_the_guard_is_installed_once_however_often_it_is_asked_for():
    """Twice-wrapped is a bug that only shows up as a doubled limit."""
    first = install_receive_guard()
    second = install_receive_guard()
    assert first is False or second is False
    assert hasattr(SecureConnection._receive, "__wrapped__")
    assert not hasattr(SecureConnection._receive.__wrapped__, "__wrapped__")


def test_the_client_advertises_the_bounds_rather_than_no_limit():
    """python-opcua defaults both to 0, which means "send me anything"."""

    class FakeClient:
        max_chunkcount = 0
        max_messagesize = 0

    client = FakeClient()
    advertise_limits(client)
    assert client.max_chunkcount == MAX_CHUNK_COUNT
    assert client.max_messagesize == MAX_MESSAGE_SIZE


def test_the_real_client_factory_advertises_them():
    """The knobs are useless if nothing sets them on the client that connects."""
    source = (
        ROOT / "packages" / "server-python" / "src" / "opcua_mcp_server" / "security.py"
    ).read_text(encoding="utf-8")
    assert "advertise_limits(client)" in source, (
        "create_client must advertise the transport bounds, or the Hello goes out "
        "with python-opcua's unlimited defaults"
    )


class HeaderOnlySocket:
    """A hostile peer providing a header, with no body to allocate or wait for."""

    def __init__(self, size, message_type=ua.MessageType.SecureMessage):
        self.data = struct.pack("<3scI", message_type, ua.ChunkType.Single, size)
        if message_type in (ua.MessageType.SecureMessage, ua.MessageType.SecureOpen):
            self.data += struct.pack("<I", 0)
        self.reads = []

    def read(self, size):
        self.reads.append(size)
        assert size <= len(self.data), "the hostile body must not be read"
        data, self.data = self.data[:size], self.data[size:]
        return data


@pytest.mark.parametrize(
    "kind",
    [
        ua.MessageType.SecureMessage,
        ua.MessageType.SecureOpen,
        ua.MessageType.Acknowledge,
        ua.MessageType.Error,
    ],
)
def test_oversize_chunk_is_rejected_before_reading_its_body(kind):
    connection = _connection()
    connection._receive(_chunk(ua.ChunkType.Intermediate))
    peer = HeaderOnlySocket(MAX_CHUNK_SIZE + 1, kind)
    with pytest.raises(ua.UaError, match="maxChunkSize"):
        connection.receive_from_socket(peer)
    assert peer.reads == (
        [8, 4] if kind in (ua.MessageType.SecureMessage, ua.MessageType.SecureOpen) else [8]
    )
    assert connection._incoming_parts == []


def test_oversize_decoded_chunk_is_rejected_too():
    chunk = _chunk(ua.ChunkType.Single)
    chunk.MessageHeader.packet_size = MAX_CHUNK_SIZE + 1
    with pytest.raises(ua.UaError, match="maxChunkSize"):
        _connection()._receive(chunk)


def test_cumulative_message_size_is_checked_before_the_next_body(monkeypatch):
    monkeypatch.setattr(limits, "MAX_MESSAGE_SIZE", 56)
    connection = _connection()
    # Each chunk fits independently: 24 bytes of headers + 4 bytes of body.
    for seq in (1, 2):
        connection._receive(_chunk(ua.ChunkType.Intermediate, sequence=seq))
    peer = HeaderOnlySocket(28)
    with pytest.raises(ua.UaError, match="maxMessageSize"):
        connection.receive_from_socket(peer)
    assert peer.reads == [8, 4]
    assert connection._incoming_parts == []


def test_message_size_boundary_and_counter_reset(monkeypatch):
    monkeypatch.setattr(limits, "MAX_MESSAGE_SIZE", 56)
    connection = _connection()
    assert connection._receive(_chunk(ua.ChunkType.Intermediate)) is None
    assert connection._receive(_chunk(ua.ChunkType.Single, sequence=2)) is not None
    assert connection._receive(_chunk(ua.ChunkType.Single, request_id=2, sequence=3)) is not None


def test_abort_releases_the_message_size_budget(monkeypatch):
    monkeypatch.setattr(limits, "MAX_MESSAGE_SIZE", 64)
    connection = _connection()
    connection._receive(_chunk(ua.ChunkType.Intermediate))
    abort = _chunk(ua.ChunkType.Abort, sequence=2)
    from opcua.ua.ua_binary import struct_to_binary

    abort.Body = struct_to_binary(ua.ErrorMessage())
    assert connection._receive(abort) is None
    assert connection._incoming_parts == []
    assert connection._receive(_chunk(ua.ChunkType.Single, request_id=2, sequence=3)) is not None


def test_invalid_negative_body_size_is_rejected():
    with pytest.raises(ua.UaError, match="Invalid OPC UA chunk size"):
        _connection().receive_from_socket(HeaderOnlySocket(8))
