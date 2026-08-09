"""Covers transports/django_ws.py's DjangoWsTransport: environment-based URL
resolution (Phase 2's xparo_environment ROS param), the single
websocket-client-driven reconnect mechanism (item 6 -- no more racing ad hoc
reconnect-on-send-failure), and "hybrid" mode's REST fallback (item 4).
"""
from unittest.mock import MagicMock, patch

import pytest

from xparo.transports.base import Transport
from xparo.transports.django_ws import DjangoWsTransport


def _make_transport(**kwargs):
    kwargs.setdefault("on_message", MagicMock())
    return DjangoWsTransport("secret", "proj-transport-test", **kwargs)


def test_is_a_transport():
    assert isinstance(_make_transport(), Transport)


def test_defaults_to_production_environment():
    transport = _make_transport()
    assert transport.socket_full_url.startswith("wss://xparo.in/")
    assert transport.website_base_url == "https://xparo.in"


def test_local_environment_override():
    transport = _make_transport(environment="local")
    assert transport.socket_full_url.startswith("ws://127.0.0.1:8000/")
    assert transport.website_base_url == "http://127.0.0.1:8000"


def test_effective_transport_non_hybrid_is_itself():
    for connection_type in ("websocket", "rest", "offline"):
        transport = _make_transport(connection_type=connection_type)
        assert transport._effective_transport() == connection_type


def test_effective_transport_hybrid_tracks_fallback_state():
    transport = _make_transport(connection_type="hybrid")
    assert transport._effective_transport() == "websocket"
    transport.rest_fallback_active = True
    assert transport._effective_transport() == "rest"


def test_connect_passes_reconnect_delay_to_run_forever():
    """websocket-client's own run_forever(reconnect=...) is now the single
    reconnect mechanism -- this is what actually fixes "reconnect on clean
    idle close, not just a failed send" (item 6), so it must be wired up on
    every connect() call.
    """
    from xparo.transports import django_ws

    captured = {}

    def fake_thread(target=None, kwargs=None, **_):
        captured['target'] = target
        captured['kwargs'] = kwargs or {}
        return MagicMock()

    with patch.object(django_ws, 'Xparo_socket', return_value=MagicMock()), \
         patch.object(django_ws.threading, 'Thread', side_effect=fake_thread):
        transport = _make_transport(connection_type="websocket")
        transport.connect()

    assert captured['kwargs'] == {"reconnect": django_ws.RECONNECT_DELAY_SECONDS}


def test_send_failure_does_not_trigger_manual_reconnect():
    """Old behavior called self.connect() again from inside send()'s except
    block -- that raced with run_forever(reconnect=...)'s own retry loop
    (two concurrent Xparo_socket/run_forever threads for one logical
    connection). It must not do that anymore.
    """
    transport = _make_transport(connection_type="websocket")
    transport.ws = MagicMock()
    transport.ws.send.side_effect = RuntimeError("socket is dead")

    with patch.object(transport, 'connect') as mock_connect:
        transport.send('{"k": "v"}', command_for="websocket")
        mock_connect.assert_not_called()


def test_on_ws_error_marks_disconnected():
    """Before self.ws.sock exists (or for rest/offline modes, which never
    create it), on_ws_error's assignment is what websocket_connected reads
    -- see test_websocket_connected_tracks_live_socket_state below for the
    websocket/hybrid case once a real socket object is attached.
    """
    transport = _make_transport()
    transport.websocket_connected = True
    transport.on_ws_error(None, RuntimeError("boom"))
    assert transport.websocket_connected is False


def test_on_ws_open_marks_connected_and_clears_fallback_and_fires_callback():
    on_connected = MagicMock()
    transport = _make_transport(connection_type="hybrid", on_connected=on_connected)
    transport.websocket_connected = False
    transport.rest_fallback_active = True

    transport.on_ws_open(None)

    assert transport.websocket_connected is True
    assert transport.rest_fallback_active is False
    on_connected.assert_called_once()


def test_websocket_connected_tracks_live_socket_state():
    """Regression test for a bug found via a real chaos test (kill Daphne
    mid-session, ros_packages/src/xparo -- Phase 8): websocket-client's
    run_forever(reconnect=N) only calls on_error/on_close for the very
    first failed attempt ever; a *second* real outage (after at least one
    successful reconnect) is retried completely silently, so a transport
    that only updates websocket_connected from those two callbacks gets
    stuck reporting True through that second outage -- exactly the signal
    _hybrid_watchdog_loop needs to be correct. Once self.ws.sock exists,
    the property must instead reflect that object's own .connected
    attribute (which websocket-client itself keeps accurate on every
    connect/disconnect, regardless of which callbacks it fires), not the
    manually-tracked fallback.
    """
    transport = _make_transport()
    assert transport.websocket_connected is False  # no self.ws yet -> fallback

    transport.ws = MagicMock()
    transport.ws.sock = MagicMock(connected=True)
    assert transport.websocket_connected is True

    # The scenario on_ws_error/on_ws_close can't reliably catch: a second
    # drop, handled entirely inside websocket-client's own retry loop with
    # neither callback firing. websocket-client updates .connected itself
    # regardless -- confirm the property follows it, not a stale flag.
    transport.ws.sock.connected = False
    assert transport.websocket_connected is False

    transport.ws.sock.connected = True
    assert transport.websocket_connected is True


def test_websocket_connected_setter_is_fallback_only_once_ws_exists():
    """on_ws_error/on_ws_open's plain assignments must not fight with the
    live socket reading once self.ws.sock exists -- they're harmless
    bookkeeping for the fallback value, not authoritative.
    """
    transport = _make_transport()
    transport.ws = MagicMock()
    transport.ws.sock = MagicMock(connected=True)

    transport.on_ws_error(None, RuntimeError("boom"))  # sets the fallback False

    assert transport.websocket_connected is True  # live socket wins


def test_hybrid_rest_fallback_loop_exits_immediately_once_inactive():
    transport = _make_transport(connection_type="hybrid")
    transport.rest_fallback_active = False
    # Must return immediately (no network call, no blocking) -- this is
    # what lets the watchdog's fallback thread wind down once on_ws_open
    # flips rest_fallback_active back to False.
    with patch('xparo.transports.django_ws.requests.get') as mock_get:
        transport._hybrid_rest_fallback_loop()
    mock_get.assert_not_called()


def test_hybrid_rest_fallback_loop_polls_and_dispatches_until_recovered():
    on_message = MagicMock()
    transport = _make_transport(connection_type="hybrid", on_message=on_message)
    transport.rest_fallback_active = True

    mock_response = MagicMock(status_code=201)
    mock_response.json.return_value = {"some": "payload"}

    def fake_get(*args, **kwargs):
        # Stop after exactly one poll cycle, like on_ws_open would once the
        # websocket recovers mid-fallback.
        transport.rest_fallback_active = False
        return mock_response

    with patch('xparo.transports.django_ws.requests.get', side_effect=fake_get), \
         patch('xparo.transports.django_ws.time.sleep'):
        transport._hybrid_rest_fallback_loop()

    on_message.assert_called_once_with('rest', {"some": "payload"})
