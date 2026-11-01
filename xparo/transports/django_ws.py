"""Transport for networked robots: WebSocket primary against the Django
backend (apps/DASH_app/consumers_API.py's API_app), with a plain REST-polling
mode and a "hybrid" mode (websocket with automatic REST fallback during a
sustained outage) alongside it. This is engine.py's Engine's only transport
today -- was inline in xparo.py before the transports/ split (see base.py's
Transport ABC docstring for why the split exists).
"""
import json
import socket
import threading
import time

import requests
import websocket

from .base import Transport

# Was a hardcoded `local = True` in the old xparo.py, silently pointing
# every deployment at 127.0.0.1:8000 unless someone edited the file
# directly -- promoted to the xparo_environment ROS param (xparo_ros.py),
# defaulting to "production" so a real robot that never sets it reaches
# xparo.in, not a dev sandbox that doesn't exist for it.
DEFAULT_ENVIRONMENT = "production"

# First retry delay of _run_websocket_loop, the single reconnect mechanism
# (see send()'s except block for why a second, ad hoc one used to race
# with it). Doubles on every failed attempt in a row, up to
# MAX_RECONNECT_DELAY_SECONDS, and drops back to this after a connection
# that actually opened -- a Wi-Fi blip still recovers in 5 s, while a
# wrong/frozen key doesn't hit the server every 5 s forever.
RECONNECT_DELAY_SECONDS = 5
MAX_RECONNECT_DELAY_SECONDS = 60

# "hybrid" connection_type: roughly how many of websocket-client's own
# reconnect cycles (each RECONNECT_DELAY_SECONDS apart) to let pass before
# falling back to REST polling. Framed as a cycle count rather than a raw
# duration because it reads the same way as "N consecutive failed
# reconnects" -- but implemented as elapsed time (see _hybrid_watchdog_loop)
# since run_forever(reconnect=...) retries silently in the background and
# doesn't call on_close/on_error again per failed attempt to count from.
MAX_CONSECUTIVE_WS_FAILURES_BEFORE_REST_FALLBACK = 5

# Deliberately much slower than start_reset_framework's plain "rest" mode
# poll (0.2s) -- this only runs during a websocket outage, and polling that
# fast from every affected robot at once is exactly the kind of thing that
# turns a brief server hiccup into a thundering-herd pile-on.
HYBRID_REST_FALLBACK_POLL_INTERVAL_SECONDS = 2


class Xparo_socket(websocket.WebSocketApp):
    def __init__(self, *args, **kwargs):
        super(Xparo_socket, self).__init__(*args, **kwargs)


class DjangoWsTransport(Transport):
    """connection_type: "websocket" (persistent, auto-reconnecting) |
    "rest" (poll-only, no websocket at all) | "hybrid" (websocket primary,
    falls back to REST polling during a sustained outage, resumes
    websocket automatically) | "offline" (no network at all).
    """

    def __init__(self, secret_key, project_id, on_message, on_connected=None,
                 connection_type="websocket", environment=None,
                 on_persisted_credential_rejected=None):
        super().__init__(on_message, on_connected)
        self.connection_type = connection_type
        # Backing store for the websocket_connected property below -- used
        # as-is for "rest"/"offline" modes (which never create self.ws) and
        # as the pre-connect/optimistic value for "websocket"/"hybrid"
        # before self.ws.sock exists.
        self._websocket_connected = False
        # "hybrid" mode bookkeeping -- see connect()/on_ws_open and
        # _hybrid_watchdog_loop/_hybrid_rest_fallback_loop below. Unused
        # (and harmless) for the other connection_type values.
        self.rest_fallback_active = False
        # Set by engine.py only when secret_key here came from a persisted
        # ROBOT_CREDENTIAL (config/credential.json), never for a raw
        # xparo_secret_key launch argument -- see on_ws_error below for why
        # that distinction matters. None means "nothing to fall back to,
        # a 403 here is just a genuinely wrong/revoked secret."
        self.on_persisted_credential_rejected = on_persisted_credential_rejected
        # What the handshake presents, for on_ws_error's 403 message --
        # engine.py only wires the callback above for a persisted
        # ROBOT_CREDENTIAL, so its presence is that distinction.
        self.secret_kind = "this robot's own credential" if on_persisted_credential_rejected else "the project secret key"
        self.project_id = project_id
        # _run_websocket_loop bookkeeping: close() sets _closed and _stop
        # (the loop's between-attempts wait is interruptible, so a close()
        # during a long backoff doesn't leave a thread sleeping); on_ws_open
        # sets _opened so the loop knows whether to reset its backoff.
        self._closed = False
        self._stop = threading.Event()
        self._opened = False

        environment = environment or DEFAULT_ENVIRONMENT
        is_local = environment == "local"
        self.server_host = '127.0.0.1:8000' if is_local else 'xparo.in'
        website_url = ("http" if is_local else "https") + '://'+self.server_host
        socket_url = ("ws" if is_local else "wss") + '://'+self.server_host
        # Bare origin (no /chatbot_api/... suffix) -- engine.py needs this
        # separately for XP_Database's BlackboxOrchestrator, which uploads
        # against a different path on the same host.
        self.website_base_url = website_url
        self.website_full_url = website_url +'/chatbot_api/'+secret_key+'/'+project_id+'/'
        self.socket_full_url = socket_url + '/ws/chatbot_api/'+str(secret_key)+'/'+str(project_id)+'/'

    @property
    def websocket_connected(self):
        # Once self.ws.sock exists, read websocket-client's own per-socket
        # .connected flag straight from it -- the library keeps it accurate
        # on every state change regardless of which callbacks fire (chaos
        # testing against a real killed Daphne process showed a flag
        # maintained only from on_ws_open/on_ws_error/on_ws_close going
        # stale through a real outage -- exactly the signal
        # _hybrid_watchdog_loop needs to be correct). Fall back to the
        # manually-tracked value for "rest"/"offline" modes (which never
        # create self.ws) and for the brief window before self.ws.sock
        # exists.
        sock = getattr(getattr(self, "ws", None), "sock", None)
        if sock is not None:
            return bool(getattr(sock, "connected", False))
        return self._websocket_connected

    @websocket_connected.setter
    def websocket_connected(self, value):
        self._websocket_connected = value

    #########################################################################################
    def connect(self):
        print('''

        connencting to ...
        ██╗░░██╗██████╗░░█████╗░██████╗░░█████╗░
        ╚██╗██╔╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗
        ░╚███╔╝░██████╔╝███████║██████╔╝██║░░██║
        ░██╔██╗░██╔═══╝░██╔══██║██╔══██╗██║░░██║
        ██╔╝╚██╗██║░░░░░██║░░██║██║░░██║╚█████╔╝
        ╚═╝░░╚═╝╚═╝░░░░░╚═╝░░╚═╝╚═╝░░╚═╝░╚════╝░

        ''')
        print("the AI Engine")
        # "hybrid" opens a websocket exactly like "websocket" does -- the
        # only difference is _hybrid_watchdog_loop, started below, which
        # falls back to REST polling if it stays down too long.
        if self.connection_type=="websocket" or self.connection_type=="hybrid":
            if not self.websocket_connected:
                self._closed = False
                self._stop.clear()
                self.websocket_connected = True
                # daemon: this loop must never keep the process alive
                # after the node shuts down (Ctrl+C / ros2 launch stop) --
                # as a plain thread it did, still connected and
                # heartbeating, until something SIGKILLed it.
                threading.Thread(target=self._run_websocket_loop, daemon=True).start()
                if self.connection_type=="hybrid":
                    threading.Thread(target=self._hybrid_watchdog_loop, daemon=True).start()
            else:
                print("already connected to xparo remote")
        elif self.connection_type=="rest":
            response = requests.get(self.website_full_url)
            if response.status_code == 201:
                data = response.json()
                self.on_message('self.ws',data)
                threading.Thread(target=self.start_reset_framework, daemon=True).start()
            else:
                print("no response")
        elif self.connection_type=="offline":
            print("offline mode with custom llm is comming soon...")
        ###################################################
        #### getting initial data..

    def _run_websocket_loop(self):
        """The reconnect mechanism. Opens the websocket and reopens it
        whenever it closes -- the server dropping it (a deploy, a key
        change), a refused handshake (wrong/frozen key: 403), the network
        going away -- until close() is called.

        websocket-client's own run_forever(reconnect=N) used to do this
        and was dropped for a reason confirmed in the 2026-10-31 field
        test: it retries silently (on_error fires for the very first
        failure only), so a credential rejected mid-session -- the key it
        was issued with got frozen -- was retried with the same 403 every
        5 s forever, the fallback to the project secret key never ran, and
        the log showed nothing but "socket is already closed." until
        someone restarted the robot. Here every attempt is a fresh
        run_forever(), so every failure reaches on_ws_error, and the wait
        between attempts backs off while they keep failing.
        """
        failures = 0
        while not self._closed:
            self._opened = False
            self.ws = Xparo_socket(str(self.socket_full_url),
                            on_message=self.on_message,
                            on_error=self.on_ws_error,
                            on_open=self.on_ws_open,
                            on_close=self.on_ws_close,
                            )
            self.ws.run_forever()
            if self._closed:
                break
            failures = 0 if self._opened else failures + 1
            delay = min(RECONNECT_DELAY_SECONDS * 2 ** max(failures - 1, 0), MAX_RECONNECT_DELAY_SECONDS)
            print(f"reconnecting to the XPARO server in {delay} s ...")
            if self._stop.wait(delay):
                break

    def close(self):
        # Stops _run_websocket_loop (and the attempt it's in the middle
        # of: closing the socket is how websocket-client's run_forever is
        # made to return). Safe to call whether or not connect() ever
        # actually got as far as creating self.ws (e.g. "rest"/"offline"
        # modes, or a transport that's being retired before its first
        # connect() attempt).
        self._closed = True
        self._stop.set()
        self.rest_fallback_active = False
        if getattr(self, "ws", None) is not None:
            self.ws.close()
        self.websocket_connected = False

    def force_reconnect(self):
        """Drops the current websocket so run_forever(reconnect=...) opens a
        fresh one on whatever network the robot is on now. Used after a
        Wi-Fi switch (connectivity.py): the old socket is bound to the old
        network's address and is dead, but with no keepalive ping it would
        keep looking connected until TCP gave up on it, minutes later.
        Unlike close(), this leaves the retry loop running."""
        raw_socket = getattr(getattr(getattr(self, "ws", None), "sock", None), "sock", None)
        if raw_socket is None:
            return
        try:
            raw_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already gone -- the retry loop is on it either way

    def _effective_transport(self):
        """connection_type=="hybrid" isn't itself a valid transport -- it's
        websocket when connected, REST while _hybrid_rest_fallback_loop is
        active. Every other connection_type already is its own transport.
        """
        if self.connection_type != "hybrid":
            return self.connection_type
        return "rest" if self.rest_fallback_active else "websocket"

    def send(self,message,command_for=None):
        if not command_for:
            command_for=self._effective_transport()
        try:
            if command_for=="websocket":
                ws = getattr(self, "ws", None)
                if ws is None or not getattr(getattr(ws, "sock", None), "connected", False):
                    # Not connected (yet, or right now): dropped quietly.
                    # This used to print "'DjangoWsTransport' object has no
                    # attribute 'ws'" at every start (Engine's own threads
                    # send before connect() opened the socket) and "socket
                    # is already closed." for every send during a
                    # reconnect -- noise that reads as errors. The caller
                    # already treats a not-connected transport as "not
                    # delivered" (engine.py's _try_send_dict).
                    return False
                ws.send(message)
            elif command_for=="rest":
                response = requests.post(self.website_full_url, data=message,headers={'Content-type': 'application/json'})
                if response.status_code == 201:
                    self.on_message('rest', response.json())
                    return True
                else:
                    print(str(response))
            elif command_for=="offline":
                pass
        except Exception as e:
            print(e)
            # No manual reconnect here -- calling connect() again would
            # race with websocket-client's own
            # run_forever(reconnect=RECONNECT_DELAY_SECONDS) loop (it would
            # spin up a *second* Xparo_socket/run_forever thread on top of
            # the first one, which is still alive and retrying on its
            # own). on_ws_close/on_ws_error already cover "the socket
            # died" via that loop; a failed send here just means this one
            # message is lost, same as it would be mid-reconnect either way.

    def on_ws_error(self, ws, error):
        # Harmless best-effort bookkeeping for the "rest"/"offline"/
        # pre-connect fallback value of websocket_connected; not relied on
        # for websocket/hybrid liveness once self.ws.sock exists.
        self.websocket_connected = False
        # Say what actually went wrong. Every failure used to print the
        # same "check your internet connection / download the latest
        # version / switch to rest" advice -- for a 403 (a wrong, frozen or
        # deleted key, a retired robot) that sent operators chasing the
        # network instead of the key (2026-10-31 field test, AARAV-3/
        # CHAOS-7).
        status = getattr(error, 'status_code', None)
        if status == 403:
            print(f"The XPARO server rejected {self.secret_kind} for project {self.project_id} "
                  f"(403 Forbidden): the secret key is wrong, frozen or deleted, or this robot was "
                  f"retired. Check Secret Key Manager and the Robots page on the dashboard -- "
                  f"this is not a network problem. ({error})")
            # A 403 while using a persisted ROBOT_CREDENTIAL means that
            # credential is no longer valid server-side (its row was reset
            # or rotated, or the key it was issued with is frozen) --
            # engine.py then retries with the xparo_secret_key this launch
            # was actually given, which the stale credential.json was
            # silently overriding. Once per transport: the swap retires
            # this one.
            if self.on_persisted_credential_rejected is not None:
                callback = self.on_persisted_credential_rejected
                self.on_persisted_credential_rejected = None
                callback()
            return
        if status is not None:
            hint = ("check xparo_project_id -- the server knows no such project." if status == 404
                    else "it is probably being deployed or is overloaded; retrying.")
            print(f"The XPARO server answered {status} during the handshake: {hint} ({error})")
            return
        if not isinstance(error, BaseException):
            # websocket-client hands the server's own close frame here
            # when the server ends the connection (a deploy, a key change).
            print("The XPARO server closed the connection -- reconnecting.")
            return
        if isinstance(error, websocket.WebSocketConnectionClosedException):
            print(f"Lost the connection to the XPARO server ({error}) -- reconnecting.")
            return
        if isinstance(error, (websocket.WebSocketException, OSError)):
            print(f"Can't reach the XPARO server at {self.server_host} ({error}). Check the robot's "
                  f"internet connection; for a local Django server launch with xparo_environment:=local.")
            return
        # Anything else was raised inside on_message/on_open (websocket-
        # client routes those here too): a bug in a handler, not the network.
        print(f"Error while handling a server message: {error!r}")

    def on_ws_open(self, ws, *args):
        self.websocket_connected = True
        self._opened = True  # resets _run_websocket_loop's backoff
        self.rest_fallback_active = False  # no-op unless hybrid fallback was active
        print('''
        \\\\Connection Successful//
           \\\\X.P.A.R.O remote//
            \\\\is 🄻🄸🅅🄴 now//
        ''')
        self.on_connected()

    def on_ws_close(self, ws, *args):
        self.websocket_connected = False
        # (close_status_code, close_reason) when the server sent a close
        # frame -- e.g. 4403 is "your key/credential was revoked".
        code = args[0] if args else None
        print(f'''

            xparo brain is
        █▀▀ █── █▀▀█ █▀▀ █▀▀ █▀▀▄
        █── █── █──█ ▀▀█ █▀▀ █──█
        ▀▀▀ ▀▀▀ ▀▀▀▀ ▀▀▀ ▀▀▀ ▀▀▀─
            {"stopped" if self._closed else "reconnecting ..."}{f" (server close code {code})" if code else ""}

        ''')

    def _hybrid_watchdog_loop(self):
        """Only started for connection_type=="hybrid" (see connect()).
        Falls back to REST polling once the websocket has been down for
        roughly MAX_CONSECUTIVE_WS_FAILURES_BEFORE_REST_FALLBACK reconnect
        cycles' worth of time, and stops the fallback again the moment
        on_ws_open reports the websocket is back (checked here, not via a
        callback, since there's nothing to reliably drive this off of --
        see on_ws_error's docstring for why).
        """
        fallback_threshold_seconds = MAX_CONSECUTIVE_WS_FAILURES_BEFORE_REST_FALLBACK * RECONNECT_DELAY_SECONDS
        disconnected_since = None
        while True:
            time.sleep(RECONNECT_DELAY_SECONDS)
            if self.websocket_connected:
                disconnected_since = None
                continue
            if disconnected_since is None:
                disconnected_since = time.monotonic()
            elif not self.rest_fallback_active and (time.monotonic() - disconnected_since) >= fallback_threshold_seconds:
                self.rest_fallback_active = True
                threading.Thread(target=self._hybrid_rest_fallback_loop, daemon=True).start()

    def _hybrid_rest_fallback_loop(self):
        print(f"hybrid mode: websocket has been down for over {MAX_CONSECUTIVE_WS_FAILURES_BEFORE_REST_FALLBACK} "
              "reconnect attempts -- falling back to REST polling until it recovers")
        while self.rest_fallback_active:
            try:
                response = requests.get(self.website_full_url)
                if response.status_code == 201:
                    self.on_message('rest', response.json())
            except Exception as e:
                print(e)
            time.sleep(HYBRID_REST_FALLBACK_POLL_INTERVAL_SECONDS)
        print("hybrid mode: websocket recovered -- stopping REST fallback")

    def start_reset_framework(self):
        print("starting reset framework")
        check = self.send(json.dumps({"initiliaze":True}))
        if check:
            while True:
                response = requests.get(self.website_full_url)
                if response.status_code == 201:
                    data = response.json()
                    self.on_message('self.ws',data)
                time.sleep(0.2)
        else:
            print("unable to connect with X.P.A.R.O server")
            self.on_ws_close('self.ws')
