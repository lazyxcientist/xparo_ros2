"""Transport for a tethered ROV/AUV with no path to Django at all while
submerged -- the physical RS-485<->Ethernet tether to a topside laptop/GCS
is the only link. Ported from tethered_module's jetson_tcp_node_d_m.py's
TcpChannel/TcpClientNode: 13-byte CRC32 framing, 3-channel link redundancy
with sticky active/secondary/backup role promotion, and a 20s app-level
silence backstop behind OS-level TCP keepalive.

NOT ported here (out of scope for a generic transport, specific to that
particular ROV's hardware): IMU/depth/pressure/battery/leak/GPS telemetry,
the emergency-stop hardware action itself, rosbag-control-via-topic. Total
link loss is still detected and reported (on_link_lost/on_link_restored
callbacks below) -- what to actually DO about it (e.g. publish an
emergency-stop message) is left to whatever ROV-specific code sits above
this transport, not hardcoded into it.

Engine's dispatch table (engine.py's on_ws_message) is written against the
Transport ABC (transports/base.py), not against this class directly -- the
same RUN_COMMAND/TELEOP/file-transfer handlers in remote_ops.py work
identically whether django_ws or this transport delivered the message.

Simplification versus the original: FILE_CHUNK payloads travel as base64
inside a PKT_JSON-framed {"FILE_CHUNK": {"data": ...}} dispatch message at
the Python level (uniform with django_ws, which can only carry JSON text),
but this transport's send()/_dispatch_packet still frame them as raw
PKT_FILE_CHUNK binary packets *on the wire* -- the base64 round-trip is a
Python-side, in-memory-only cost, not extra bytes actually sent over the
tether. FILE_REQ/FILE_COMPLETE (small control messages, not bulk bytes)
are just sent as PKT_JSON, unlike the original's separate PKT_FILE_REQ/
PKT_FILE_COMPLETE type bytes -- that distinction only ever mattered for
routing at the original's C-level dispatch; here on_message already routes
by dispatch-key regardless of which packet type produced it.
"""
import base64
import json
import select
import socket
import struct
import threading
import time
import zlib
from queue import Queue, Empty

from .base import Transport

# ----------------------------------------------------------------------
# Redundant tether channels, each a separate RS-485<->Ethernet bridge
# pair. Priority is index order: 0 = highest priority (preferred "active"
# channel whenever it's connected), last = lowest (pure backup). Override
# via the channels_config constructor arg -- these are just a reasonable
# default for local/loopback testing (see test_bridge_server-style setups),
# not a real deployment's actual host/port pairs.
# ----------------------------------------------------------------------
DEFAULT_CHANNELS_CONFIG = [
    {"name": "CH-A", "host": "127.0.0.1", "port": 7001},
    {"name": "CH-B", "host": "127.0.0.1", "port": 7002},
    {"name": "CH-C", "host": "127.0.0.1", "port": 7003},
]

RECV_BUFFER = 65536

# How often the recv loop wakes up (via select()) to re-check link
# liveness, independent of the socket's blocking/timeout mode.
RECV_POLL_INTERVAL_SEC = 2.0
# App-level silence backstop, behind OS-level TCP keepalive -- see
# TcpChannel._connect_loop() for why both layers exist.
LINK_DEAD_TIMEOUT_SEC = 20.0
HEARTBEAT_INTERVAL_SEC = 2.0
RECONNECT_BACKOFF_SEC = 5.0
ROLE_UPDATE_INTERVAL_SEC = 0.5

PKT_HEARTBEAT      = 0x01
PKT_HEARTBEAT_ACK  = 0x02
PKT_JSON           = 0x10
PKT_FILE_CHUNK     = 0x12
MAGIC = b"ROVC"
HEADER_SIZE = 13  # 4 MAGIC + 1 type + 4 len + 4 crc32


def pack_message(msg_type: int, payload: bytes) -> bytes:
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return MAGIC + struct.pack("!BII", msg_type, len(payload), crc) + payload


def pack_json(msg_type: int, obj: dict) -> bytes:
    return pack_message(msg_type, json.dumps(obj).encode("utf-8"))


# ----------------------------------------------------------------------
# TcpChannel - one independently-reconnecting tether link
# ----------------------------------------------------------------------
class TcpChannel:
    """One of the redundant tether channels. Owns its own socket, its own
    reconnect loop (OS keepalive tuning identical to the original), and
    its own send/recv queues.

    `send_queue` is None whenever the channel is disconnected (so callers
    can tell "no session to send into" apart from "session exists but
    empty") and is replaced with a fresh Queue() on every successful
    connect -- anything queued right before a drop is discarded rather
    than flushed stale into the next session.
    """

    def __init__(self, transport: "TetheredTcpTransport", name: str, host: str, port: int, priority: int):
        self.transport = transport
        self.name = name
        self.host = host
        self.port = port
        self.priority = priority

        self.sock = None
        self.send_queue: "Queue | None" = None
        self.recv_queue: "Queue" = Queue()

        self.connected = False
        self.last_rx = 0.0
        self.reconnects = 0
        self.role = "down"  # "active" | "secondary" | "backup" | "down"

    def start(self):
        threading.Thread(target=self._connect_loop, daemon=True, name=f"{self.name}-conn").start()
        threading.Thread(target=self._dispatch_loop, daemon=True, name=f"{self.name}-disp").start()

    def send(self, data: bytes) -> bool:
        q = self.send_queue
        if q is not None:
            q.put(data)
            return True
        return False

    def status(self) -> dict:
        age = None if self.last_rx == 0.0 else max(0.0, time.monotonic() - self.last_rx)
        return {
            "name": self.name, "host": self.host, "port": self.port,
            "connected": self.connected, "role": self.role,
            "last_rx_age": age, "reconnects": self.reconnects,
        }

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    # ---------- connection lifecycle ----------
    def _connect_loop(self):
        t = self.transport
        while not t._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((self.host, self.port))
                # Back to fully blocking -- select() in _recv_loop polls
                # for readability instead of socket-level settimeout(),
                # which would also cap sendall() and misfire on a
                # slow-but-healthy send (e.g. a large file chunk).
                sock.settimeout(None)

                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 3)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                except (AttributeError, OSError):
                    pass  # keepalive tuning isn't available on this platform

                self.sock = sock
                self.send_queue = Queue()
                self.last_rx = time.monotonic()
                self.connected = True

                active = threading.Event()
                active.set()
                send_thread = threading.Thread(target=self._send_loop, args=(sock, active), daemon=True)
                recv_thread = threading.Thread(target=self._recv_loop, args=(sock, active), daemon=True)
                send_thread.start()
                recv_thread.start()

                while active.is_set() and not t._stop_event.is_set():
                    time.sleep(0.2)

                active.clear()
                self.connected = False
                self.send_queue = None
                send_thread.join(timeout=1)
                recv_thread.join(timeout=1)
                sock.close()
                self.sock = None
                self.reconnects += 1
                if not t._stop_event.is_set():
                    time.sleep(RECONNECT_BACKOFF_SEC)

            except (socket.error, Exception):
                self.connected = False
                self.send_queue = None
                if not t._stop_event.is_set():
                    time.sleep(RECONNECT_BACKOFF_SEC)

    def _send_loop(self, sock, active):
        while active.is_set() and not self.transport._stop_event.is_set():
            try:
                data = self.send_queue.get(timeout=0.5)
                sock.sendall(data)
            except Empty:
                continue
            except (BrokenPipeError, ConnectionResetError, OSError):
                active.clear()
                break

    def _recv_loop(self, sock, active):
        buffer = bytearray()
        while active.is_set() and not self.transport._stop_event.is_set():
            try:
                ready, _, _ = select.select([sock], [], [], RECV_POLL_INTERVAL_SEC)
                if not ready:
                    if time.monotonic() - self.last_rx > LINK_DEAD_TIMEOUT_SEC:
                        active.clear()
                        break
                    continue

                chunk = sock.recv(RECV_BUFFER)
                if not chunk:
                    active.clear()
                    break
                self.last_rx = time.monotonic()
                buffer.extend(chunk)
                while len(buffer) >= HEADER_SIZE:
                    if buffer[:4] != MAGIC:
                        buffer.pop(0)
                        continue
                    msg_type, payload_len, expected_crc = struct.unpack("!BII", buffer[4:13])
                    total = HEADER_SIZE + payload_len
                    if len(buffer) < total:
                        break
                    payload = bytes(buffer[HEADER_SIZE:total])
                    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
                    if actual_crc != expected_crc:
                        # False sync -- MAGIC bytes appeared inside a
                        # previous payload. Skip one byte and keep scanning.
                        buffer.pop(0)
                        continue
                    del buffer[:total]
                    self.recv_queue.put((msg_type, payload))
            except (ConnectionResetError, BrokenPipeError, OSError):
                active.clear()
                break

    def _dispatch_loop(self):
        """Single consumer of recv_queue for the channel's lifetime -- a
        second consumer on the same queue was the root cause of duplicate
        acks and binary chunk payloads crashing the JSON decoder in an
        earlier version of the code this was ported from."""
        while not self.transport._stop_event.is_set():
            try:
                msg_type, payload = self.recv_queue.get(timeout=0.5)
            except Empty:
                continue
            self.transport._dispatch_packet(self, msg_type, payload)


class TetheredTcpTransport(Transport):
    def __init__(self, on_message, on_connected=None, channels_config=None,
                 on_link_lost=None, on_link_restored=None):
        super().__init__(on_message, on_connected)
        # Unlike django_ws, there's no "connection succeeded" moment for a
        # multi-channel link in the same sense -- on_connected fires once,
        # the first time ANY channel comes up (see _update_roles), not per
        # reconnect; on_link_lost/on_link_restored below are what track
        # ongoing availability after that.
        self.on_link_lost = on_link_lost or (lambda: None)
        self.on_link_restored = on_link_restored or (lambda: None)

        self.channels = [
            TcpChannel(self, cfg["name"], cfg["host"], cfg["port"], priority)
            for priority, cfg in enumerate(channels_config or DEFAULT_CHANNELS_CONFIG)
        ]
        self._active_name = None
        self._secondary_name = None
        self._forced_active = None
        self._roles_lock = threading.Lock()
        self._link_ever_up = False
        self._link_lost_latched = False
        self._stop_event = threading.Event()

    def connect(self):
        for ch in self.channels:
            ch.start()
        threading.Thread(target=self._role_update_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def close(self):
        self._stop_event.set()
        for ch in self.channels:
            ch.close()

    def send(self, message, command_for=None):
        """`message` is a JSON string (matches every engine.py call site).
        `command_for` may name a specific channel (reply on the same link
        a request arrived on -- see _dispatch_packet); defaults to the
        current active channel.
        """
        try:
            obj = json.loads(message)
        except (TypeError, ValueError):
            return False
        channel = self._channel_named(command_for) or self._active_channel()
        if channel is None:
            return False
        if list(obj.keys()) == ["FILE_CHUNK"]:
            # Bulk bytes -- framed as raw PKT_FILE_CHUNK on the wire, not
            # PKT_JSON, despite arriving here as base64-in-JSON (see this
            # module's docstring).
            data = base64.b64decode(obj["FILE_CHUNK"].get("data", ""))
            return channel.send(pack_message(PKT_FILE_CHUNK, data))
        return channel.send(pack_json(PKT_JSON, obj))

    def _channel_named(self, name):
        if not name:
            return None
        for ch in self.channels:
            if ch.name == name:
                return ch
        return None

    def _active_channel(self):
        return self._channel_named(self._active_name)

    def force_active(self, name):
        """Operator override (e.g. a GCS "Force" button) -- pin the active
        channel to `name`, or clear back to automatic selection if `name`
        is falsy/unknown. Only takes effect once that channel is actually
        connected; see _role_update_loop."""
        valid = {c.name for c in self.channels}
        with self._roles_lock:
            self._forced_active = name if name in valid else None

    # ---------- channel role management ----------
    def _role_update_loop(self):
        while not self._stop_event.is_set():
            time.sleep(ROLE_UPDATE_INTERVAL_SEC)
            self._update_roles()

    def _update_roles(self):
        """Recompute which channel is "active" (carries outbound command/
        reply traffic) and which is "secondary" (mirrored traffic, a live
        second feed). Any other connected channel is a warm "backup".

        Sticky: a channel keeps its active/secondary role even after a
        higher-priority channel reconnects -- only replaced once it itself
        disconnects. Avoids flapping on a marginal link.
        """
        with self._roles_lock:
            connected = sorted((c for c in self.channels if c.connected), key=lambda c: c.priority)
            connected_names = {c.name for c in connected}

            was_up = self._active_name is not None
            if self._forced_active is not None and self._forced_active in connected_names:
                self._active_name = self._forced_active
            elif self._active_name not in connected_names:
                self._active_name = connected[0].name if connected else None

            sec_candidates = [c for c in connected if c.name != self._active_name]
            sec_names = {c.name for c in sec_candidates}
            if self._secondary_name not in sec_names:
                self._secondary_name = sec_candidates[0].name if sec_candidates else None

            active_name = self._active_name
            secondary_name = self._secondary_name

        for ch in self.channels:
            if not ch.connected:
                ch.role = "down"
            elif ch.name == active_name:
                ch.role = "active"
            elif ch.name == secondary_name:
                ch.role = "secondary"
            else:
                ch.role = "backup"

        if active_name is not None:
            if not was_up:
                if not self._link_ever_up:
                    self.on_connected()
                else:
                    self.on_link_restored()
            self._link_ever_up = True
            self._link_lost_latched = False
        elif self._link_ever_up and not self._link_lost_latched:
            self._link_lost_latched = True
            self.on_link_lost()

    def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            time.sleep(HEARTBEAT_INTERVAL_SEC)
            for ch in self.channels:
                ch.send(pack_message(PKT_HEARTBEAT, b""))

    # ---------- packet dispatch ----------
    def _dispatch_packet(self, channel: TcpChannel, msg_type, payload):
        try:
            if msg_type == PKT_HEARTBEAT:
                channel.send(pack_message(PKT_HEARTBEAT_ACK, b""))
            elif msg_type == PKT_HEARTBEAT_ACK:
                pass
            elif msg_type == PKT_JSON:
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                self.on_message(channel.name, obj)
            elif msg_type == PKT_FILE_CHUNK:
                self.on_message(channel.name, {"FILE_CHUNK": {
                    "data": base64.b64encode(payload).decode("ascii"),
                }})
        except Exception:
            # One bad packet must never kill the channel.
            pass
