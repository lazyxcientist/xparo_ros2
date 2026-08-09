"""Pluggable connection strategy for engine.py's Engine.

django_ws.py (networked robots -- WebSocket primary against the Django
backend, with REST-polling and hybrid fallback modes) is the only
implementation today. Phase 4 of the XPARO fleet-unification plan adds
tethered_tcp.py alongside it (RS-485-tethered ROV, no path to Django at all
while submerged) -- Engine's dispatch table (on_ws_message's key-routing)
is written against this interface, not against either transport directly,
so the exact same dispatch logic drives both without caring which one is
active.
"""
from abc import ABC, abstractmethod


class Transport(ABC):
    def __init__(self, on_message, on_connected=None):
        # on_message(source, message) -- called by the transport whenever a
        # message arrives, regardless of which underlying wire format
        # produced it. Engine passes its own on_ws_message here; `source`
        # is transport-specific and only used for logging (django_ws passes
        # the Xparo_socket instance, or the literal string 'rest').
        self.on_message = on_message
        # on_connected() -- called once a connection is usable, so Engine
        # can trigger its initial handshake (requesting robot info, synced
        # config, etc.) without the transport needing to know what that
        # handshake looks like.
        self.on_connected = on_connected or (lambda: None)

    @abstractmethod
    def connect(self):
        """Start connecting. Must not block forever -- spin up background
        threads as needed and return control to the caller."""
        raise NotImplementedError

    @abstractmethod
    def send(self, message, command_for=None):
        """Send a message. `message` is a JSON string (matches every
        existing call site in engine.py). `command_for` is an optional
        transport-specific routing hint (django_ws uses it to pick
        websocket vs. REST vs. offline; transports that only have one way
        to send can ignore it)."""
        raise NotImplementedError
