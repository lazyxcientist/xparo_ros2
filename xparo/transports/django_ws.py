"""Transport for networked robots: WebSocket primary against the Django
backend (apps/DASH_app/consumers_API.py's API_app), with a plain REST-polling
mode and a "hybrid" mode (websocket with automatic REST fallback during a
sustained outage) alongside it. This is engine.py's Engine's only transport
today -- was inline in xparo.py before the transports/ split (see base.py's
Transport ABC docstring for why the split exists).
"""
import json
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

# Passed to websocket-client's own run_forever(reconnect=...) -- the single
# reconnect mechanism (see send()'s except block for why a second, ad hoc
# one used to race with it).
RECONNECT_DELAY_SECONDS = 5

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

        environment = environment or DEFAULT_ENVIRONMENT
        is_local = environment == "local"
        website_url = ("http" if is_local else "https") + '://'+('127.0.0.1:8000' if is_local else 'xparo.in')
        socket_url = ("ws" if is_local else "wss") + '://'+('127.0.0.1:8000' if is_local else 'xparo.in')
        # Bare origin (no /chatbot_api/... suffix) -- engine.py needs this
        # separately for XP_Database's BlackboxOrchestrator, which uploads
        # against a different path on the same host.
        self.website_base_url = website_url
        self.website_full_url = website_url +'/chatbot_api/'+secret_key+'/'+project_id+'/'
        self.socket_full_url = socket_url + '/ws/chatbot_api/'+str(secret_key)+'/'+str(project_id)+'/'

    @property
    def websocket_connected(self):
        # websocket-client's run_forever(reconnect=N) only calls on_error
        # and on_close for the very first failed attempt / the socket's
        # very first open -- every disconnect after that (whether the
        # established connection drops, or a later reconnect attempt
        # itself fails) is retried silently with neither callback firing
        # (confirmed against websocket-client 1.9.0's handleDisconnect,
        # which only invokes on_error when the *initialize_socket* call
        # that failed was not itself already a reconnect). Chaos testing
        # this against a real killed Daphne process showed that relying on
        # on_ws_open/on_ws_error/on_ws_close alone leaves this flag stuck
        # True through a second, real outage even though the socket is
        # down and silently retrying -- exactly the signal
        # _hybrid_watchdog_loop needs to be correct. WebSocket.connected
        # (the *raw* per-attempt flag inside websocket-client's own
        # WebSocket object, distinct from this property) is updated
        # directly by that library on every state change regardless of
        # which callbacks it decides to fire, so read it straight from
        # there once it exists; fall back to the manually-tracked value
        # for "rest"/"offline" modes (which never create self.ws) and for
        # the brief window before self.ws.sock exists.
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
                self.ws = Xparo_socket(str(self.socket_full_url),
                                on_message=self.on_message,
                                on_error=self.on_ws_error,
                                on_open=self.on_ws_open,
                                on_close=self.on_ws_close,
                                )
                self.websocket_connected = True
                # reconnect=RECONNECT_DELAY_SECONDS makes this loop retry on
                # ANY disconnect (clean idle close included, not just a
                # failed send) -- the single reconnect mechanism now; see
                # send()'s except block.
                threading.Thread(target=self.ws.run_forever,
                                  kwargs={"reconnect": RECONNECT_DELAY_SECONDS}).start()
                if self.connection_type=="hybrid":
                    threading.Thread(target=self._hybrid_watchdog_loop, daemon=True).start()
            else:
                print("already connected to xparo remote")
        elif self.connection_type=="rest":
            response = requests.get(self.website_full_url)
            if response.status_code == 201:
                data = response.json()
                self.on_message('self.ws',data)
                threading.Thread(target=self.start_reset_framework).start()
            else:
                print("no response")
        elif self.connection_type=="offline":
            print("offline mode with custom llm is comming soon...")
        ###################################################
        #### getting initial data..

    def close(self):
        # Stops run_forever's own retry loop (sets its keep_running flag
        # and closes the underlying socket) -- websocket-client has no
        # separate "stop reconnecting" call, closing is the one mechanism.
        # Safe to call whether or not connect() ever actually got as far as
        # creating self.ws (e.g. "rest"/"offline" modes, or a transport
        # that's being retired before its first connect() attempt).
        self.rest_fallback_active = False
        if getattr(self, "ws", None) is not None:
            self.ws.close()
        self.websocket_connected = False

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
                self.ws.send(message)
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
        # websocket-client's run_forever(reconnect=N) only calls on_error
        # once, for the very first failed attempt -- every silent retry
        # after that calls neither on_error nor on_close (see the
        # websocket_connected property's docstring for how that's actually
        # handled now). This assignment is harmless best-effort bookkeeping
        # for the "rest"/"offline"/pre-connect fallback value; it is not
        # relied on for websocket/hybrid liveness once self.ws.sock exists.
        self.websocket_connected = False
        print(error)

        # A 403 on the very first attempt, specifically while using a
        # persisted ROBOT_CREDENTIAL, means that credential is no longer
        # valid server-side (its robot/credential row was deleted or
        # rotated) but the stale config/credential.json is still on disk,
        # silently overriding whatever xparo_secret_key was actually passed
        # to this launch. Without this, run_forever(reconnect=N) just keeps
        # retrying the same doomed URL forever -- confirmed happening for
        # real: a perfectly valid secret_key argument produced a permanent
        # 403 loop with no indication it was never actually tried. Only
        # fire once (on_ws_error's own "first attempt only" behavior
        # already gives us that), and only when engine.py told us this
        # secret came from the persisted file in the first place -- a
        # genuinely wrong/revoked project secret typed by a user should
        # still just fail normally, nothing to fall back to there.
        if self.on_persisted_credential_rejected is not None and getattr(error, 'status_code', None) == 403:
            callback = self.on_persisted_credential_rejected
            self.on_persisted_credential_rejected = None
            callback()
            return

        print(f'''
        Truble shooting:
            1. check your internet connection
            2. if that not working download latest version of xparo or from github = https://github.com/lazyxcientist/xparo
            3. try to switch to websocket connection or rest framework
        ''')

    def on_ws_open(self, ws, *args):
        self.websocket_connected = True
        self.rest_fallback_active = False  # no-op unless hybrid fallback was active
        print('''
        \\\\Connection Sussessfull//
           \\\\X.P.A.R.O remote//
            \\\\is 🄻🄸🅅🄴 now//
        ''')
        self.on_connected()

    def on_ws_close(self, ws, *args):
        self.websocket_connected = False
        print('''

            xparo brain is
        █▀▀ █── █▀▀█ █▀▀ █▀▀ █▀▀▄
        █── █── █──█ ▀▀█ █▀▀ █──█
        ▀▀▀ ▀▀▀ ▀▀▀▀ ▀▀▀ ▀▀▀ ▀▀▀─
            retry again !!!

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
