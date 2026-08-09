"""Covers transports/tethered_tcp.py against real TCP loopback sockets
(not mocks) -- framing/CRC/reconnect/redundancy is exactly the kind of
logic mocking would paper over. Each test spins up a minimal fake
topside-GCS TCP server, points a TetheredTcpTransport at it, and asserts
on real bytes exchanged over a real socket.
"""
import base64
import json
import socket
import struct
import threading
import time
import zlib

import pytest

from xparo.transports.base import Transport
from xparo.transports.tethered_tcp import (
    HEADER_SIZE, MAGIC, PKT_FILE_CHUNK, PKT_JSON,
    TetheredTcpTransport, pack_json, pack_message,
)


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeGcsServer:
    """One-shot fake topside server: accepts exactly one connection, records
    every framed packet it receives, and lets the test push bytes back.
    """
    def __init__(self):
        self.port = _free_port()
        self.received = []
        self._conn = None
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(('127.0.0.1', self.port))
        self._srv.listen(5)
        self._srv.settimeout(5)
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        try:
            conn, _ = self._srv.accept()
        except socket.timeout:
            return
        self._conn = conn
        buffer = bytearray()
        conn.settimeout(5)
        while not self._stop:
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            buffer.extend(chunk)
            while len(buffer) >= HEADER_SIZE:
                if buffer[:4] != MAGIC:
                    buffer.pop(0)
                    continue
                msg_type, plen, _crc = struct.unpack("!BII", buffer[4:13])
                total = HEADER_SIZE + plen
                if len(buffer) < total:
                    break
                payload = bytes(buffer[HEADER_SIZE:total])
                del buffer[:total]
                self.received.append((msg_type, payload))

    def wait_for_connection(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self._conn is None and time.monotonic() < deadline:
            time.sleep(0.02)
        return self._conn

    def send_raw(self, data: bytes):
        self._conn.sendall(data)

    def close(self):
        self._stop = True
        if self._conn:
            try:
                self._conn.close()
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass


@pytest.fixture
def gcs_server():
    server = FakeGcsServer()
    yield server
    server.close()


def _make_transport(server, **kwargs):
    return TetheredTcpTransport(
        channels_config=[{"name": "CH-A", "host": "127.0.0.1", "port": server.port}],
        **kwargs,
    )


def _wait_for_active(transport, timeout=5):
    deadline = time.monotonic() + timeout
    while transport._active_name is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return transport._active_name


def test_is_a_transport(gcs_server):
    assert isinstance(_make_transport(gcs_server, on_message=lambda s, m: None), Transport)


def test_connect_calls_on_connected_once_link_comes_up(gcs_server):
    connected = threading.Event()
    transport = _make_transport(gcs_server, on_message=lambda s, m: None, on_connected=connected.set)
    transport.connect()
    try:
        assert connected.wait(timeout=5)
        assert transport._active_name == "CH-A"
        assert transport.channels[0].role == "active"
    finally:
        transport.close()


def test_send_frames_a_regular_message_as_pkt_json(gcs_server):
    transport = _make_transport(gcs_server, on_message=lambda s, m: None)
    transport.connect()
    try:
        gcs_server.wait_for_connection()
        assert _wait_for_active(transport) == "CH-A"
        ok = transport.send(json.dumps({"RUN_COMMAND": {"command": "echo hi", "request_id": "r1"}}))
        assert ok is True
        time.sleep(0.3)
        assert len(gcs_server.received) == 1
        msg_type, payload = gcs_server.received[0]
        assert msg_type == PKT_JSON
        assert json.loads(payload.decode()) == {"RUN_COMMAND": {"command": "echo hi", "request_id": "r1"}}
    finally:
        transport.close()


def test_send_frames_file_chunk_as_raw_binary_not_json(gcs_server):
    """The whole point of keeping PKT_FILE_CHUNK as a distinct binary
    packet type -- bulk file bytes shouldn't pay JSON/base64 overhead on
    the actual wire, even though they arrive here as base64-in-JSON at the
    Python level (see this module's docstring)."""
    transport = _make_transport(gcs_server, on_message=lambda s, m: None)
    transport.connect()
    try:
        gcs_server.wait_for_connection()
        assert _wait_for_active(transport) == "CH-A"
        raw = b"some raw file bytes"
        ok = transport.send(json.dumps({"FILE_CHUNK": {"data": base64.b64encode(raw).decode()}}))
        assert ok is True
        time.sleep(0.3)
        msg_type, payload = gcs_server.received[0]
        assert msg_type == PKT_FILE_CHUNK
        assert payload == raw  # raw bytes on the wire, not a JSON/base64 envelope
    finally:
        transport.close()


def test_receives_and_decodes_json_packets(gcs_server):
    messages = []
    transport = _make_transport(gcs_server, on_message=lambda source, msg: messages.append((source, msg)))
    transport.connect()
    try:
        conn = gcs_server.wait_for_connection()
        assert conn is not None
        conn.sendall(pack_json(PKT_JSON, {"COMMAND_RESULT": {"request_id": "x", "success": True}}))
        time.sleep(0.3)
        assert messages == [("CH-A", {"COMMAND_RESULT": {"request_id": "x", "success": True}})]
    finally:
        transport.close()


def test_receives_raw_file_chunk_as_base64_dispatch_message(gcs_server):
    messages = []
    transport = _make_transport(gcs_server, on_message=lambda source, msg: messages.append((source, msg)))
    transport.connect()
    try:
        conn = gcs_server.wait_for_connection()
        conn.sendall(pack_message(PKT_FILE_CHUNK, b"chunk-bytes"))
        time.sleep(0.3)
        assert len(messages) == 1
        source, msg = messages[0]
        assert base64.b64decode(msg["FILE_CHUNK"]["data"]) == b"chunk-bytes"
    finally:
        transport.close()


def test_corrupted_packet_is_skipped_and_next_valid_one_still_decodes(gcs_server):
    """CRC mismatch means the MAGIC bytes were a false sync (appeared by
    chance inside a previous payload) -- must resync byte-by-byte and keep
    going, not desync the whole stream."""
    messages = []
    transport = _make_transport(gcs_server, on_message=lambda source, msg: messages.append((source, msg)))
    transport.connect()
    try:
        conn = gcs_server.wait_for_connection()
        corrupted = bytearray(pack_json(PKT_JSON, {"BOGUS": 1}))
        corrupted[-1] ^= 0xFF
        good = pack_json(PKT_JSON, {"REAL": True})
        conn.sendall(bytes(corrupted) + good)
        time.sleep(0.3)
        assert messages == [("CH-A", {"REAL": True})]
    finally:
        transport.close()


def test_heartbeat_gets_acked(gcs_server):
    from xparo.transports.tethered_tcp import PKT_HEARTBEAT_ACK
    transport = _make_transport(gcs_server, on_message=lambda s, m: None)
    transport.connect()
    try:
        conn = gcs_server.wait_for_connection()
        conn.sendall(pack_message(0x01, b""))  # PKT_HEARTBEAT
        time.sleep(0.3)
        assert any(msg_type == PKT_HEARTBEAT_ACK for msg_type, _ in gcs_server.received)
    finally:
        transport.close()


def test_two_channels_sticky_active_after_reconnect():
    server_a = FakeGcsServer()
    server_b = FakeGcsServer()
    try:
        transport = TetheredTcpTransport(
            on_message=lambda s, m: None,
            channels_config=[
                {"name": "CH-A", "host": "127.0.0.1", "port": server_a.port},
                {"name": "CH-B", "host": "127.0.0.1", "port": server_b.port},
            ],
        )
        transport.connect()
        try:
            deadline = time.monotonic() + 5
            while transport._active_name != "CH-A" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert transport._active_name == "CH-A"

            server_a.wait_for_connection()
            server_a._conn.close()
            server_a._stop = True

            deadline = time.monotonic() + 5
            while transport._active_name != "CH-B" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert transport._active_name == "CH-B"

            # CH-A's own client-side reconnect loop will keep retrying
            # against a now-closed server and fail -- that's fine, the
            # point is CH-B must not be displaced even if it did reconnect.
            time.sleep(1.5)
            assert transport._active_name == "CH-B"
        finally:
            transport.close()
    finally:
        server_a.close()
        server_b.close()
