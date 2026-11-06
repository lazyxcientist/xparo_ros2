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


def test_on_ws_error_403_fires_persisted_credential_callback_once():
    """Regression test for a bug found via a real local-testing session
    (Phase 8): a persisted ROBOT_CREDENTIAL that's since been invalidated
    server-side (its Robots/RobotCredential row deleted or rotated) gets
    rejected with a 403 on the very first handshake attempt -- and without
    this callback, run_forever(reconnect=N) would just keep retrying that
    exact same doomed URL forever, so a perfectly valid xparo_secret_key
    argument would never actually get tried. on_ws_error only ever fires
    once per real outage (see websocket_connected's own docstring), so the
    callback must fire on that first 403 and then never again for this
    transport instance.
    """
    callback = MagicMock()
    transport = _make_transport(on_persisted_credential_rejected=callback)
    error = MagicMock()
    error.status_code = 403

    transport.on_ws_error(None, error)
    assert callback.call_count == 1
    assert transport.on_persisted_credential_rejected is None

    # A second error on the same (now stale) transport must not re-fire it.
    transport.on_ws_error(None, error)
    assert callback.call_count == 1


def test_on_ws_error_non_403_does_not_fire_persisted_credential_callback():
    """A genuinely wrong/revoked project secret (or any other handshake
    failure) has nothing to fall back to -- only a 403 specifically while
    using a persisted credential should trigger the swap.
    """
    callback = MagicMock()
    transport = _make_transport(on_persisted_credential_rejected=callback)

    transport.on_ws_error(None, RuntimeError("connection refused"))
    callback.assert_not_called()

    error = MagicMock()
    error.status_code = 500
    transport.on_ws_error(None, error)
    callback.assert_not_called()


def test_on_ws_error_403_without_callback_does_not_raise():
    """A raw xparo_secret_key launch argument (not a persisted credential)
    wires on_persisted_credential_rejected=None -- a 403 there is just a
    genuinely bad secret and must fall through to the normal error path
    without blowing up on a None call.
    """
    transport = _make_transport()  # on_persisted_credential_rejected defaults to None
    error = MagicMock()
    error.status_code = 403
    transport.on_ws_error(None, error)  # must not raise


def test_close_stops_the_websocket_and_marks_disconnected():
    transport = _make_transport(connection_type="hybrid")
    transport.rest_fallback_active = True
    fake_ws = MagicMock()
    # websocket_connected reads straight from ws.sock.connected once it
    # exists (see test_websocket_connected_tracks_live_socket_state) --
    # simulate the socket actually being down, same as a real close()
    # would leave it, rather than asserting against the manually-tracked
    # fallback value close() also sets but which this property ignores
    # whenever a live ws.sock is present.
    fake_ws.sock = MagicMock(connected=False)
    transport.ws = fake_ws

    transport.close()

    fake_ws.close.assert_called_once()
    assert transport.websocket_connected is False
    assert transport.rest_fallback_active is False


def test_close_before_connect_does_not_raise():
    transport = _make_transport()  # never had connect() called, no self.ws yet
    transport.close()  # must not raise


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


def test_a_closed_hybrid_transports_watchdog_stops_instead_of_polling_rest():
    """The engine swaps transports (credential <-> project key) for as long
    as both are refused (CHAOS-23). A swapped-out hybrid transport's watchdog
    never stopped: it saw the closed socket as "down", started REST polling
    on the old key and polled it every 2 s forever -- one more per swap."""
    import threading

    transport = _make_transport(connection_type="hybrid")
    transport.close()
    with patch('xparo.transports.django_ws.requests.get') as mock_get, \
         patch('xparo.transports.django_ws.time.monotonic', side_effect=[0.0] + [1e6] * 1000):
        watchdog = threading.Thread(target=transport._hybrid_watchdog_loop, daemon=True)
        watchdog.start()
        watchdog.join(timeout=2)
        assert not watchdog.is_alive()
    assert transport.rest_fallback_active is False
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


# ---- 2026-10-31 field test: the reconnect loop and its messages -------------

class _ClosingSocket:
    """A fake Xparo_socket whose run_forever returns at once -- what
    websocket-client's does after a refused handshake or a server-side
    close now that its own reconnect= retry isn't used."""
    created = []

    def __init__(self, url, on_message=None, on_error=None, on_open=None, on_close=None):
        self.url = url
        self.sock = None
        self.on_open = on_open
        self.on_close = on_close
        self.on_error = on_error
        _ClosingSocket.created.append(self)

    def run_forever(self, **kwargs):
        self.on_close(self)

    def close(self):
        pass


def _loop_with_fake_socket(transport, fake_socket, stop_after):
    """Runs _run_websocket_loop with the waits recorded instead of slept;
    closes the transport after stop_after waits."""
    from xparo.transports import django_ws
    _ClosingSocket.created = []
    delays = []

    def fake_wait(delay):
        delays.append(delay)
        if len(delays) >= stop_after:
            transport.close()
        return transport._closed

    with patch.object(django_ws, 'Xparo_socket', fake_socket), \
         patch.object(transport._stop, 'wait', side_effect=fake_wait):
        transport._run_websocket_loop()
    return delays


def test_connect_starts_the_supervised_websocket_loop():
    """_run_websocket_loop is the reconnect mechanism (websocket-client's
    own run_forever(reconnect=...) was dropped -- see the loop's
    docstring), so connect() must start it, once."""
    from xparo.transports import django_ws
    targets = []

    with patch.object(django_ws.threading, 'Thread',
                      side_effect=lambda target=None, **_: targets.append(target) or MagicMock()):
        transport = _make_transport(connection_type="websocket")
        transport.connect()

    assert targets == [transport._run_websocket_loop]


def test_loop_reopens_the_socket_with_backoff_while_the_server_keeps_refusing():
    """SANDEEP-5 / CHAOS-7: after the server dropped a robot (key freeze)
    websocket-client retried the same doomed handshake silently every 5 s
    -- no on_error, no fallback, nothing in the log. Every attempt is now
    a fresh socket (so every failure reaches on_ws_error), and the wait
    between failed attempts doubles up to MAX_RECONNECT_DELAY_SECONDS."""
    from xparo.transports import django_ws
    transport = _make_transport(connection_type="websocket")

    delays = _loop_with_fake_socket(transport, _ClosingSocket, stop_after=6)

    assert len(_ClosingSocket.created) == 6
    assert delays == [5, 10, 20, 40, 60, 60]
    assert delays[-1] == django_ws.MAX_RECONNECT_DELAY_SECONDS


def test_backoff_resets_after_a_connection_that_actually_opened():
    """A Wi-Fi blip after hours online must still recover in 5 s."""
    class _OpensThenCloses(_ClosingSocket):
        def run_forever(self, **kwargs):
            self.on_open(self)
            self.on_close(self)

    transport = _make_transport(connection_type="websocket")
    assert _loop_with_fake_socket(transport, _OpensThenCloses, stop_after=3) == [5, 5, 5]


def test_a_connection_the_server_refuses_still_backs_off():
    """The server opens the socket, answers robot_retired /
    robot_credential_required and closes it with 4403. That opened, but
    resetting the backoff on it made a retired robot re-handshake every
    5 s forever."""
    from xparo.transports import django_ws

    class _OpensThenRefuses(_ClosingSocket):
        def run_forever(self, **kwargs):
            self.on_open(self)
            self.on_close(self, django_ws.SERVER_REFUSED_CLOSE_CODE, "refused")

    transport = _make_transport(connection_type="websocket")
    transport.on_connected = lambda: None
    assert _loop_with_fake_socket(transport, _OpensThenRefuses, stop_after=4) == [5, 10, 20, 40]


def test_a_refusal_delivered_as_a_close_frame_still_backs_off():
    """websocket-client 1.9 hands the server's close frame to on_error and
    then calls on_close(None, None): the 4403 must be read from the frame,
    or a retired robot re-handshakes every 5 s (2026-11-03)."""
    import websocket
    from xparo.transports import django_ws

    class _OpensThenRefusesByFrame(_ClosingSocket):
        def run_forever(self, **kwargs):
            self.on_open(self)
            frame = websocket.ABNF(opcode=websocket.ABNF.OPCODE_CLOSE,
                                   data=django_ws.SERVER_REFUSED_CLOSE_CODE.to_bytes(2, "big") + b"refused")
            self.on_error(self, frame)
            self.on_close(self, None, None)

    transport = _make_transport(connection_type="websocket")
    transport.on_connected = lambda: None
    assert _loop_with_fake_socket(transport, _OpensThenRefusesByFrame, stop_after=4) == [5, 10, 20, 40]


def test_a_normal_close_frame_does_not_count_as_a_refusal():
    import websocket
    from xparo.transports import django_ws
    assert django_ws._close_code_of(websocket.ABNF(opcode=websocket.ABNF.OPCODE_CLOSE, data=(1000).to_bytes(2, "big"))) == 1000
    assert django_ws._close_code_of(websocket.ABNF(opcode=websocket.ABNF.OPCODE_TEXT, data=b"hi")) is None


def test_close_stops_the_loop_for_good():
    from xparo.transports import django_ws
    transport = _make_transport(connection_type="websocket")
    transport.close()
    _ClosingSocket.created = []
    with patch.object(django_ws, 'Xparo_socket', _ClosingSocket):
        transport._run_websocket_loop()
    assert _ClosingSocket.created == []


def test_a_403_names_the_key_not_the_internet(capsys):
    """AARAV-3 / CHAOS-7: a wrong, frozen or deleted key printed "check
    your internet connection / download the latest version / switch to
    rest" and sent operators chasing the network."""
    transport = _make_transport()
    transport.on_ws_error(None, MagicMock(status_code=403))
    out = capsys.readouterr().out
    assert "rejected the project secret key" in out
    assert "403" in out and "Secret Key Manager" in out
    assert "internet" not in out and "Truble" not in out


def test_a_403_while_using_a_persisted_credential_says_so(capsys):
    transport = _make_transport(on_persisted_credential_rejected=MagicMock())
    transport.on_ws_error(None, MagicMock(status_code=403))
    assert "rejected this robot's own credential" in capsys.readouterr().out


def test_a_network_error_still_points_at_the_connection(capsys):
    transport = _make_transport()
    transport.on_ws_error(None, ConnectionRefusedError(111, "Connection refused"))
    out = capsys.readouterr().out
    assert "Can't reach the XPARO server at xparo.in" in out
    assert "internet connection" in out


def test_a_send_before_the_socket_exists_is_dropped_quietly(capsys):
    """CHAOS-3 / RO-6: every start printed "'DjangoWsTransport' object has
    no attribute 'ws'" -- Engine's own threads send before connect() has
    opened the socket."""
    transport = _make_transport(connection_type="websocket")
    assert transport.send('{"k": "v"}') is False
    assert capsys.readouterr().out == ""


def test_a_send_while_reconnecting_is_dropped_quietly(capsys):
    transport = _make_transport(connection_type="websocket")
    transport.ws = MagicMock()
    transport.ws.sock = MagicMock(connected=False)
    assert transport.send('{"k": "v"}') is False
    transport.ws.send.assert_not_called()
    assert capsys.readouterr().out == ""


def test_success_banner_is_spelled_right(capsys):
    transport = _make_transport()
    transport.on_ws_open(None)
    transport.on_server_accepted()
    out = capsys.readouterr().out
    assert "Connection Successful" in out and "Sussessfull" not in out


def test_the_live_banner_waits_for_the_server_to_accept_the_robot(capsys):
    """AARAV-16 / SANDEEP-13: every retry of a refused robot (credential
    required, serial in use) printed "Connection Successful ... is LIVE
    now" when the socket opened, right before the server's refusal -- a
    beginner read that as online."""
    transport = _make_transport()
    transport.on_ws_open(None)
    out = capsys.readouterr().out
    assert "Connection Successful" not in out and "waiting for it to accept this robot" in out

    transport.on_server_accepted()
    transport.on_server_accepted()  # later replies on the same connection
    assert capsys.readouterr().out.count("Connection Successful") == 1


def test_a_refused_connection_never_prints_the_live_banner(capsys):
    transport = _make_transport()
    transport.on_ws_open(None)
    transport.on_ws_close(None, 4403, "")
    transport.on_server_accepted()  # nothing may count once it closed
    assert "Connection Successful" not in capsys.readouterr().out


def test_startup_banner_has_no_typo_or_stray_line(capsys):
    """AARAV-3: every start printed "connencting to ..." and a stray
    "the AI Engine" line under the banner."""
    transport = _make_transport(connection_type="offline")
    transport.connect()
    out = capsys.readouterr().out
    assert "connecting to" in out
    assert "connencting" not in out and "the AI Engine" not in out
