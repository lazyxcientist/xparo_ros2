"""AdManager -- plays the robot's approved ads on schedule and reports every play.

    server --ads_schedule--> update_schedule() -> files downloaded, schedule saved
    tick() every second      -> each placement plays its due ads in turn
                                (backend.play), one at a time per placement
    backend finishes         -> exactly one play-log row per play
    upload loop              -> ADS_PLAYS batches; ads_plays_ack marks them done
                                then ADS_PROOF screenshots; ads_proof_ack
    status loop, every minute -> ADS_STATUS: is the player up, what's on screen,
                                can it take screenshots (the advertiser's uptime)

Display modes (the entry's "mode", set when the ad was booked):
    repeat  plays back to back through its slot window (the default)
    full    stays on screen until the slot window ends ("loop" for video/audio)
    once    plays a single time at the start of each slot ("slot"), never again
            in that slot -- even after a restart (the play log remembers)
    timed   like once, but closes itself after "duration" (the owner's timeout)
"close_button": false hides the viewer's close (X) button.

Proof of play: when a schedule entry has "proof": true, the first play of
that ad in each clock hour gets a screenshot of the whole screen a couple of
seconds in (capture.ScreenCapture -- the display, never the camera). It is
stored with the play's log row and sent once the play itself is stored.

A backend that plays the whole schedule itself (XP-shell through ROS2,
backends/ros2.py) gets the schedule instead of individual plays, and hands
its play records back through record_external_play(); no screenshots then.
"""
import base64
import datetime
import logging
import os
import threading
import time
import uuid

from . import scheduler
from .capture import ScreenCapture
from .playlog import PlayLog
from .store import ScheduleStore

log = logging.getLogger("xparo.ads")

TICK_SECONDS = 1.0
UPLOAD_EVERY_SECONDS = 30.0
GAP_BETWEEN_ADS_SECONDS = 0.5
DEFAULT_AD_SECONDS = 15
# If a backend never reports back (a crashed player), free the slot and log it.
WATCHDOG_EXTRA_SECONDS = 60
STATUSES = ("completed", "dismissed", "interrupted", "error")
STATUS_EVERY_SECONDS = 60.0
# A viewer closed a "repeat" or "full slot" ad: give the screen back for a while before it returns.
DISMISS_COOLDOWN_SECONDS = 30.0
MIN_FULL_SECONDS = 5.0
PROOF_DELAY_SECONDS = 2.0
# Answers after which a screenshot is dropped rather than sent again.
PROOF_RETRY_REASONS = ("play not found",)


def utc_offset_minutes():
    return int(time.localtime().tm_gmtoff // 60)


def _now_iso(dt):
    return dt.isoformat(timespec="seconds")


# The server re-sends the schedule on every connect and every night. One
# older than this means the robot has been cut off long enough that a venue
# may have withdrawn consent or a campaign been stopped since: it stops
# playing until it hears from the server again, instead of playing those
# ads on for days.
MAX_SCHEDULE_AGE_S = 36 * 3600


class AdManager:
    def __init__(self, folder, send, backend, base_url=None, now=datetime.datetime.now, capture=None):
        """folder: where the schedule, ad files and play log live.
        send(dict) -> bool: send a message to the server (True if it went out).
        backend: a backends.* display backend.
        capture: a capture.ScreenCapture (screenshots for proof of play)."""
        os.makedirs(folder, exist_ok=True)
        self.store = ScheduleStore(folder, base_url)
        self.log = PlayLog(os.path.join(folder, "plays.db"))
        self.proof_folder = os.path.join(folder, "proofs")
        self.capture = capture if capture is not None else ScreenCapture()
        self._proof_hours = set()  # (ad id, "YYYY-MM-DDTHH") already captured
        self._slots_done = set()   # (ad id, slot) of once/timed ads already shown
        self._slots_checked = set()
        self.send = send
        self.backend = backend
        self.now = now
        self.items = self.store.load().get("ads") or []
        try:
            self.schedule_at = os.path.getmtime(self.store.schedule_path)
        except OSError:
            self.schedule_at = None
        self._stale_logged = False
        self._slots = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._upload_wake = threading.Event()
        self._threads = []
        self._update_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.backend.start(self)
        if not self.backend.plays_itself:
            self.backend.schedule_changed(self.items)
        for target, name in ((self._tick_loop, "xparo-ads-tick"), (self._upload_loop, "xparo-ads-upload"),
                             (self._status_loop, "xparo-ads-status")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._stop.set()
        self._upload_wake.set()
        try:
            self.backend.stop_all()
        finally:
            self.backend.close()

    # ------------------------------------------------------- server messages
    def on_connected(self):
        """(Re)connected to the server: ask for the current schedule and
        send any plays the server hasn't acknowledged yet."""
        # utc_offset_min: ad dates are this robot's own calendar dates.
        self.send({"GET_ads_schedule": {"utc_offset_min": utc_offset_minutes()}})
        self.log.mark_unsent()
        self._upload_wake.set()

    def update_schedule(self, schedule, wait=False):
        """A new schedule from the server. Files are downloaded in the
        background; ads keep playing from the old schedule meanwhile."""
        if not isinstance(schedule, dict):
            return
        thread = threading.Thread(target=self._apply_schedule, args=(schedule,), name="xparo-ads-update", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def _apply_schedule(self, schedule):
        with self._update_lock:
            localized, missing = self.store.localize(schedule)
            for item in missing:
                log.warning("ad %s not ready yet: %s", item["id"], item["error"])
            self.store.save(localized)
            self.store.prune(localized)
            with self._lock:
                self.items = localized["ads"]
                self.schedule_at = time.time()
                self._stale_logged = False
            if not self.backend.plays_itself:
                self.backend.schedule_changed(self.items)
            else:
                # Ads that were taken off (stopped/declined) stop right away.
                live = {item.get("id") for item in self.items}
                self.backend.stop_missing(live)

    def on_proof_ack(self, ack):
        """The server's answer to one ADS_PROOF: the file is done with unless it asks to retry."""
        if not isinstance(ack, dict) or not isinstance(ack.get("local_id"), int):
            return
        if not ack.get("ok") and ack.get("reason") in PROOF_RETRY_REASONS:
            self.log.proof_unsent(ack["local_id"])
            return
        if not ack.get("ok"):
            log.warning("server refused ad screenshot %s: %s", ack["local_id"], ack.get("reason"))
        self.log.proof_done(ack["local_id"])

    def on_ack(self, ack):
        if not isinstance(ack, dict):
            return
        stored = [i for i in ack.get("ack") or [] if isinstance(i, int)]
        rejected = [r.get("local_id") for r in ack.get("rejected") or []
                    if isinstance(r, dict) and isinstance(r.get("local_id"), int)]
        for r in ack.get("rejected") or []:
            if isinstance(r, dict):
                log.warning("server refused ad play %s: %s", r.get("local_id"), r.get("reason"))
        self.log.acknowledge(stored, rejected)

    # --------------------------------------------------------------- playing
    def tick(self, now=None):
        """Start the next due ad in every idle placement."""
        if not self.backend.plays_itself or not self.backend.available:
            return
        now = now or self.now()
        with self._lock:
            if self.schedule_is_stale():
                if not self._stale_logged:
                    log.warning("ads paused: no schedule from the server for over %d hours", MAX_SCHEDULE_AGE_S // 3600)
                    self._stale_logged = True
                return
            groups = scheduler.active_by_placement(self.items, now)
            for placement, queue in groups.items():
                slot = self._slots.setdefault(placement, {"busy": False, "index": 0, "token": None})
                if slot["busy"]:
                    if slot.get("deadline") is not None and time.monotonic() > slot["deadline"]:
                        self._finish(placement, slot["token"], "error", "the player did not report back")
                    continue
                if time.monotonic() < slot.get("idle_until", 0):
                    continue
                due = [i for i in queue if not self._slot_done(i)]
                if not due:
                    continue
                item = due[slot["index"] % len(due)]
                slot["index"] = (slot["index"] + 1) % len(due)
                self._start(placement, slot, item, now)

    def schedule_is_stale(self):
        return self.schedule_at is not None and time.time() - self.schedule_at > MAX_SCHEDULE_AGE_S

    def _slot_key(self, item):
        return (item.get("id"), item.get("slot") or "")

    def _slot_done(self, item):
        """A once/timed ad already shown in this slot (asked of the play log once per slot)."""
        if item.get("mode") not in ("once", "timed"):
            return False
        key = self._slot_key(item)
        if key in self._slots_done:
            return True
        if key not in self._slots_checked:
            self._slots_checked.add(key)
            if item.get("slot_start") and self.log.played_since(item.get("id"), item["slot_start"]):
                self._slots_done.add(key)
                return True
        return False

    def _play_seconds(self, item, now):
        """How long this showing lasts: the ad's length, the owner's timeout, or the rest of the slot."""
        mode = item.get("mode")
        if mode == "full" and item.get("slot_end"):
            try:
                end = datetime.datetime.fromisoformat(item["slot_end"])
                return max(MIN_FULL_SECONDS, (end - now).total_seconds() - 1)
            except ValueError:
                pass
        return float(item.get("duration") or DEFAULT_AD_SECONDS)

    def _start(self, placement, slot, item, now):
        token = object()
        duration = self._play_seconds(item, now)
        if item.get("mode") in ("once", "timed"):
            self._slots_done.add(self._slot_key(item))
        play_item = dict(item, duration=duration)
        slot.update(busy=True, token=token, item=item, started=now, proof=None,
                    deadline=time.monotonic() + duration + WATCHDOG_EXTRA_SECONDS)
        try:
            self.backend.play(play_item, placement,
                              lambda status, error=None, _p=placement, _t=token: self._finish(_p, _t, status, error))
        except Exception as exc:  # a broken ad must never stop the others
            log.exception("could not start ad %s", item.get("id"))
            self._finish(placement, token, "error", str(exc)[:300])
            return
        hour = (item.get("id"), now.strftime("%Y-%m-%dT%H"))
        if item.get("proof") and hour not in self._proof_hours:
            timer = threading.Timer(min(PROOF_DELAY_SECONDS, duration / 2), self._take_proof,
                                    args=(placement, token, hour))
            timer.daemon = True
            timer.start()

    def _take_proof(self, placement, token, hour):
        """A screenshot while this play is still on screen, kept with its log row."""
        with self._lock:
            slot = self._slots.get(placement)
            if not slot or slot.get("token") is not token or not slot["busy"] or hour in self._proof_hours:
                return
        path = os.path.join(self.proof_folder, f"{uuid.uuid4().hex}.jpg")
        captured_at = self.now()
        try:
            ok = self.capture.grab(path)
        except Exception:
            log.exception("ad screenshot failed")
            ok = False
        if not ok:
            return
        with self._lock:
            slot = self._slots.get(placement)
            if slot and slot.get("token") is token and slot["busy"]:
                slot["proof"] = (path, _now_iso(captured_at))
                self._proof_hours.add(hour)
                if len(self._proof_hours) > 5000:
                    self._proof_hours = {h for h in self._proof_hours if h[1] >= hour[1][:10]}
                return
        try:  # the play ended while we were capturing: the picture proves nothing
            os.remove(path)
        except OSError:
            pass

    def _finish(self, placement, token, status, error=None):
        """Exactly one log row per play, however it ends."""
        with self._lock:
            slot = self._slots.get(placement)
            if not slot or slot.get("token") is not token or not slot["busy"]:
                return
            item, started, proof = slot["item"], slot["started"], slot.get("proof")
            pause = GAP_BETWEEN_ADS_SECONDS
            if status == "dismissed" and item.get("mode", "repeat") in ("repeat", "full"):
                pause = DISMISS_COOLDOWN_SECONDS
            slot.update(busy=False, token=None, item=None, deadline=None, proof=None,
                        idle_until=time.monotonic() + pause)
        ended = self.now()
        self.log.add({
            "ad_id": item.get("id"), "title": item.get("title"), "media_type": item.get("type"),
            "placement": placement, "status": status if status in STATUSES else "error",
            "play_started_at": _now_iso(started), "play_ended_at": _now_iso(ended),
            "duration_played_sec": round(max(0.0, (ended - started).total_seconds()), 2),
            "error_message": (error or "")[:300] or None,
            "proof_path": proof[0] if proof else None, "proof_captured_at": proof[1] if proof else None,
        })
        self._upload_wake.set()

    def record_external_play(self, record):
        """A play reported by a backend that runs the schedule itself (XP-shell)."""
        if not isinstance(record, dict) or not record.get("ad_id") or not record.get("play_started_at"):
            return None
        status = record.get("status")
        self._upload_wake.set()
        return self.log.add({
            "ad_id": str(record["ad_id"]), "title": record.get("title") or record.get("client"),
            "media_type": record.get("media_type"), "placement": record.get("placement"),
            "status": status if status in STATUSES else "error",
            "play_started_at": str(record["play_started_at"]), "play_ended_at": record.get("play_ended_at"),
            "duration_played_sec": record.get("duration_played_sec") or 0,
            "error_message": record.get("error_message"),
        })

    # -------------------------------------------------------------- uploads
    def upload_once(self):
        """Send one batch of unacknowledged plays. Returns how many were sent."""
        rows = [r for r in self.log.due_for_upload() if str(r.get("ad_id") or "").startswith("xparo-")]
        if not rows:
            return 0
        if not self.send({"ADS_PLAYS": {"plays": rows, "utc_offset_min": utc_offset_minutes()}}):
            self.log.mark_unsent()
            return 0
        return len(rows)

    def upload_proofs_once(self):
        """Send the screenshots of plays the server has stored. Returns how many were sent."""
        sent = 0
        for row in self.log.proofs_due():
            try:
                with open(row["proof_path"], "rb") as f:
                    image = base64.b64encode(f.read()).decode("ascii")
            except OSError:
                self.log.proof_done(row["id"])  # the file is gone; nothing to send
                continue
            ok = self.send({"ADS_PROOF": {"local_id": row["id"], "ad_id": row["ad_id"],
                                          "play_started_at": row["play_started_at"],
                                          "captured_at": row["proof_captured_at"], "image": image,
                                          "utc_offset_min": utc_offset_minutes()}})
            if not ok:
                self.log.proof_unsent(row["id"])
                break
            sent += 1
        return sent

    def status(self):
        """ADS_STATUS: is the player able to show ads, what's on screen, can it prove it."""
        with self._lock:
            playing = [slot["item"].get("id") for slot in self._slots.values() if slot.get("busy") and slot.get("item")]
        proof = bool(self.backend.plays_itself and self.backend.available and self.capture.available)
        return {"available": bool(self.backend.available), "backend": self.backend.name,
                "playing": [p for p in playing if str(p or "").startswith("xparo-")], "proof": proof,
                "utc_offset_min": utc_offset_minutes()}

    def status_once(self):
        return self.send({"ADS_STATUS": self.status()})

    def _safe_status(self):
        try:
            self.status_once()
        except Exception:
            log.exception("ad status report failed")

    # --------------------------------------------------------------- threads
    def _tick_loop(self):
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("ad tick failed")

    def _upload_loop(self):
        while not self._stop.is_set():
            self._upload_wake.wait(UPLOAD_EVERY_SECONDS)
            self._upload_wake.clear()
            if self._stop.is_set():
                return
            try:
                self.upload_once()
                self.upload_proofs_once()
            except Exception:
                log.exception("ad play upload failed")

    def _status_loop(self):
        while not self._stop.is_set():
            self._safe_status()
            self._stop.wait(STATUS_EVERY_SECONDS)
