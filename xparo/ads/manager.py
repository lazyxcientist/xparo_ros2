"""AdManager -- plays the robot's approved ads on schedule and reports every play.

    server --ads_schedule--> update_schedule() -> files downloaded, schedule saved
    tick() every second      -> each placement plays its due ads in turn
                                (backend.play), one at a time per placement
    backend finishes         -> exactly one play-log row per play
    upload loop              -> ADS_PLAYS batches; ads_plays_ack marks them done

A backend that plays the whole schedule itself (XP-shell through ROS2,
backends/ros2.py) gets the schedule instead of individual plays, and hands
its play records back through record_external_play().
"""
import datetime
import logging
import os
import threading
import time

from . import scheduler
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


def utc_offset_minutes():
    return int(time.localtime().tm_gmtoff // 60)


def _now_iso(dt):
    return dt.isoformat(timespec="seconds")


class AdManager:
    def __init__(self, folder, send, backend, base_url=None, now=datetime.datetime.now):
        """folder: where the schedule, ad files and play log live.
        send(dict) -> bool: send a message to the server (True if it went out).
        backend: a backends.* display backend."""
        os.makedirs(folder, exist_ok=True)
        self.store = ScheduleStore(folder, base_url)
        self.log = PlayLog(os.path.join(folder, "plays.db"))
        self.send = send
        self.backend = backend
        self.now = now
        self.items = self.store.load().get("ads") or []
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
        for target, name in ((self._tick_loop, "xparo-ads-tick"), (self._upload_loop, "xparo-ads-upload")):
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
            if not self.backend.plays_itself:
                self.backend.schedule_changed(self.items)
            else:
                # Ads that were taken off (stopped/declined) stop right away.
                live = {item.get("id") for item in self.items}
                self.backend.stop_missing(live)

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
            groups = scheduler.active_by_placement(self.items, now)
            for placement, queue in groups.items():
                slot = self._slots.setdefault(placement, {"busy": False, "index": 0, "token": None})
                if slot["busy"]:
                    if slot.get("deadline") is not None and time.monotonic() > slot["deadline"]:
                        self._finish(placement, slot["token"], "error", "the player did not report back")
                    continue
                if time.monotonic() < slot.get("idle_until", 0):
                    continue
                item = queue[slot["index"] % len(queue)]
                slot["index"] = (slot["index"] + 1) % len(queue)
                self._start(placement, slot, item, now)

    def _start(self, placement, slot, item, now):
        token = object()
        duration = float(item.get("duration") or DEFAULT_AD_SECONDS)
        slot.update(busy=True, token=token, item=item, started=now,
                    deadline=time.monotonic() + duration + WATCHDOG_EXTRA_SECONDS)
        try:
            self.backend.play(item, placement,
                              lambda status, error=None, _p=placement, _t=token: self._finish(_p, _t, status, error))
        except Exception as exc:  # a broken ad must never stop the others
            log.exception("could not start ad %s", item.get("id"))
            self._finish(placement, token, "error", str(exc)[:300])

    def _finish(self, placement, token, status, error=None):
        """Exactly one log row per play, however it ends."""
        with self._lock:
            slot = self._slots.get(placement)
            if not slot or slot.get("token") is not token or not slot["busy"]:
                return
            item, started = slot["item"], slot["started"]
            slot.update(busy=False, token=None, item=None, deadline=None,
                        idle_until=time.monotonic() + GAP_BETWEEN_ADS_SECONDS)
        ended = self.now()
        self.log.add({
            "ad_id": item.get("id"), "title": item.get("title"), "media_type": item.get("type"),
            "placement": placement, "status": status if status in STATUSES else "error",
            "play_started_at": _now_iso(started), "play_ended_at": _now_iso(ended),
            "duration_played_sec": round(max(0.0, (ended - started).total_seconds()), 2),
            "error_message": (error or "")[:300] or None,
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
            except Exception:
                log.exception("ad play upload failed")
