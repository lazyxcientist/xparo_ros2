"""Ads on the robot's screen (xparo/ads): which ads are due, where they go,
the play log and its upload, the schedule store, AdManager's exactly-once
logging, the native player's command lines, the ROS 2 hand-off to
XP-shell, and the Engine wiring. No display or ROS needed."""
import datetime
import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from xparo.ads import geometry, scheduler
from xparo.ads.backends import make_backend
from xparo.ads.backends.base import DisplayBackend, OffBackend
from xparo.ads.backends.native import NativeBackend, ffplay_command, probe_size, text_font_size
from xparo.ads.backends.ros2 import schedule_message
from xparo.ads.manager import AdManager, utc_offset_minutes
from xparo.ads.playlog import PlayLog
from xparo.ads.store import ScheduleStore

MON_10AM = datetime.datetime(2026, 10, 5, 10, 0)  # a Monday


def entry(ad_id="xparo-a", **overrides):
    item = {"id": ad_id, "title": "Coffee", "type": "text", "text": "2-for-1", "duration": 15,
            "placement": "bottom", "start_time": "09:00", "end_time": "12:00", "days": "all",
            "priority": 1, "enabled": True, "valid_from": "2026-10-01", "valid_until": "2026-10-10"}
    item.update(overrides)
    return item


# ------------------------------------------------------------------ scheduler
class TestScheduler:
    def test_times_days_and_dates(self):
        assert scheduler.is_active(entry(), MON_10AM)
        assert not scheduler.is_active(entry(start_time="11:00"), MON_10AM)
        assert not scheduler.is_active(entry(days=["tue"]), MON_10AM)
        assert scheduler.is_active(entry(days=["mon"]), MON_10AM)
        assert not scheduler.is_active(entry(valid_from="2026-10-06"), MON_10AM)
        assert not scheduler.is_active(entry(valid_until="2026-10-04"), MON_10AM)
        assert not scheduler.is_active(entry(enabled=False), MON_10AM)

    def test_overnight_window(self):
        late = entry(start_time="22:00", end_time="02:00")
        assert scheduler.is_active(late, MON_10AM.replace(hour=23))
        assert scheduler.is_active(late, MON_10AM.replace(hour=1))
        assert not scheduler.is_active(late, MON_10AM)

    def test_grouped_by_placement_in_priority_order(self):
        groups = scheduler.active_by_placement([
            entry("xparo-b", priority=2), entry("xparo-a", priority=1),
            entry("xparo-c", placement="left"), entry("xparo-d", placement="bogus"),
        ], MON_10AM)
        assert [i["id"] for i in groups["bottom"]] == ["xparo-a", "xparo-b"]
        assert [i["id"] for i in groups["left"]] == ["xparo-c"]
        assert [i["id"] for i in groups["center"]] == ["xparo-d"]  # unknown placement -> center


# ------------------------------------------------------------------- geometry
class TestGeometry:
    def test_fullscreen_keeps_the_ads_shape(self):
        # a 4:3 photo on a 16:9 screen: full height, centred, not stretched
        assert geometry.ad_box(1920, 1080, "fullscreen", (800, 600)) == (240, 0, 1440, 1080)

    def test_side_columns_hug_their_edge(self):
        x, y, w, h = geometry.ad_box(1920, 1080, "left", (1600, 900))
        assert (x, w) == (0, 576)                      # 30% column, full width of it
        assert round(w / h, 2) == round(1600 / 900, 2)  # same 16:9 shape
        assert y == (1080 - h) // 2 or abs(y - (1080 - h) / 2) <= 1
        x, _, w, _ = geometry.ad_box(1920, 1080, "right", (1600, 900))
        assert x + w == 1920

    def test_strips(self):
        x, y, w, h = geometry.ad_box(1920, 1080, "bottom", (1200, 200))
        assert y + h == 1080 and h <= 1080 * 0.22 + 1
        _, y, _, _ = geometry.ad_box(1920, 1080, "top", (1200, 200))
        assert y == 0

    def test_text_fills_its_region_and_audio_gets_a_card(self):
        assert geometry.ad_box(1000, 1000, "center", None, "text") == (200, 200, 600, 600)
        _, _, w, h = geometry.ad_box(1920, 1080, "center", None, "audio")
        assert (w, h) == geometry.AUDIO_CARD


# -------------------------------------------------------------------- playlog
class TestPlayLog:
    def test_upload_ack_and_resend(self, tmp_path):
        log = PlayLog(str(tmp_path / "plays.db"))
        a = log.add({"ad_id": "xparo-a", "status": "completed", "play_started_at": "2026-10-05T10:00:00"})
        b = log.add({"ad_id": "xparo-a", "status": "dismissed", "play_started_at": "2026-10-05T10:01:00"})
        rows = log.due_for_upload(now=1000)
        assert [r["local_id"] for r in rows] == [a, b]
        assert log.due_for_upload(now=1001) == []            # in flight, not re-sent at once
        assert len(log.due_for_upload(now=1000 + 121)) == 2   # no answer: sent again later
        log.acknowledge([a], [b])
        assert log.due_for_upload(now=5000) == []
        assert log.counts() == {1: 1, 2: 1}

    def test_mark_unsent_after_reconnect(self, tmp_path):
        log = PlayLog(str(tmp_path / "plays.db"))
        log.add({"ad_id": "xparo-a", "status": "completed", "play_started_at": "x"})
        log.due_for_upload(now=1000)
        log.mark_unsent()
        assert len(log.due_for_upload(now=1001)) == 1


# ---------------------------------------------------------------------- store
def fake_opener(files):
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        if url not in files:
            raise OSError("404")
        return io.BytesIO(files[url])
    opener.calls = calls
    return opener


class TestStore:
    def test_localize_downloads_once_and_skips_missing_files(self, tmp_path):
        store = ScheduleStore(str(tmp_path), base_url=lambda: "https://xparo.in")
        opener = fake_opener({"https://xparo.in/media/ad.png": b"png-bytes"})
        schedule = {"ads": [entry("xparo-a", type="image", media_url="/media/ad.png"),
                            entry("xparo-b", type="video", media_url="/media/gone.mp4"),
                            entry("xparo-c")]}
        localized, missing = store.localize(schedule, opener=opener)
        ready = localized["ads"]
        assert [i["id"] for i in ready] == ["xparo-a", "xparo-c"]
        assert missing[0]["id"] == "xparo-b"
        assert open(ready[0]["path"], "rb").read() == b"png-bytes"
        assert "media_url" not in ready[0]
        store.localize(schedule, opener=opener)
        assert opener.calls.count("https://xparo.in/media/ad.png") == 1  # cached

    def test_refuses_non_http_urls_and_prunes_old_files(self, tmp_path):
        store = ScheduleStore(str(tmp_path))
        _, missing = store.localize({"ads": [entry(type="image", media_url="file:///etc/passwd")]})
        assert missing
        stale = os.path.join(store.media_dir, "old.png")
        open(stale, "wb").close()
        store.prune({"ads": []})
        assert not os.path.exists(stale)

    def test_save_and_load(self, tmp_path):
        store = ScheduleStore(str(tmp_path))
        store.save({"ads": [entry()]})
        assert store.load()["ads"][0]["id"] == "xparo-a"
        assert ScheduleStore(str(tmp_path / "empty")).load() == {"ads": []}


# -------------------------------------------------------------------- manager
class FakePlayer(DisplayBackend):
    name = "fake"

    def __init__(self):
        self.started = []
        self.stopped_missing = []

    def play(self, item, placement, on_finished):
        self.started.append((item["id"], placement, on_finished))

    def stop_missing(self, live_ids):
        self.stopped_missing.append(set(live_ids))


class FakeDelegate(DisplayBackend):
    name = "delegate"
    plays_itself = False

    def __init__(self):
        self.schedules = []

    def schedule_changed(self, items):
        self.schedules.append(list(items))


def make_manager(tmp_path, backend, sent=None, ok=True):
    sent = sent if sent is not None else []

    def send(payload):
        sent.append(payload)
        return ok
    manager = AdManager(str(tmp_path / "ads"), send=send, backend=backend, now=lambda: MON_10AM)
    manager.backend.start(manager)
    return manager, sent


class TestManager:
    def test_one_ad_per_placement_in_turn_and_one_log_row_each(self, tmp_path):
        player = FakePlayer()
        manager, _ = make_manager(tmp_path, player)
        manager.items = [entry("xparo-a"), entry("xparo-b", priority=2), entry("xparo-c", placement="left")]
        manager.tick(MON_10AM)
        assert sorted((i, p) for i, p, _ in player.started) == [("xparo-a", "bottom"), ("xparo-c", "left")]
        manager.tick(MON_10AM)
        assert len(player.started) == 2                      # busy: nothing new starts
        _, _, done = player.started[0]
        done("completed")
        done("completed")                                    # a second report is ignored
        assert manager.log.counts() == {0: 1}
        manager._slots["bottom"]["idle_until"] = 0
        manager.tick(MON_10AM)
        assert player.started[-1][0] == "xparo-b"            # then the next ad in that slot

    def test_dismissed_and_failing_plays_are_logged(self, tmp_path):
        player = FakePlayer()
        manager, _ = make_manager(tmp_path, player)
        manager.items = [entry()]
        manager.tick(MON_10AM)
        player.started[0][2]("dismissed")
        rows = manager.log.recent()
        assert (rows[0]["status"], rows[0]["placement"]) == ("dismissed", "bottom")

        class Broken(FakePlayer):
            def play(self, item, placement, on_finished):
                raise RuntimeError("no screen")
        manager2, _ = make_manager(tmp_path / "2", Broken())
        manager2.items = [entry()]
        manager2.tick(MON_10AM)
        assert manager2.log.recent()[0]["status"] == "error"

    def test_watchdog_frees_a_stuck_slot(self, tmp_path):
        player = FakePlayer()
        manager, _ = make_manager(tmp_path, player)
        manager.items = [entry()]
        manager.tick(MON_10AM)
        manager._slots["bottom"]["deadline"] = 0
        manager.tick(MON_10AM)
        assert manager.log.recent()[0]["error_message"] == "the player did not report back"

    def test_unavailable_player_starts_nothing(self, tmp_path):
        manager, _ = make_manager(tmp_path, OffBackend())
        manager.items = [entry()]
        manager.tick(MON_10AM)
        assert manager.log.recent() == []

    def test_connect_asks_for_the_schedule_and_uploads_plays(self, tmp_path):
        player = FakePlayer()
        manager, sent = make_manager(tmp_path, player)
        manager.on_connected()
        assert sent[0] == {"GET_ads_schedule": {"utc_offset_min": utc_offset_minutes()}}
        manager.log.add({"ad_id": "xparo-a", "status": "completed", "play_started_at": "2026-10-05T10:00:00"})
        manager.log.add({"ad_id": "local-banner", "status": "completed", "play_started_at": "2026-10-05T10:00:00"})
        assert manager.upload_once() == 1                     # only XPARO's own ads go to the server
        payload = sent[-1]["ADS_PLAYS"]
        assert payload["plays"][0]["ad_id"] == "xparo-a" and "utc_offset_min" in payload
        local_id = payload["plays"][0]["local_id"]
        manager.on_ack({"ack": [local_id], "rejected": []})
        assert manager.upload_once() == 0

    def test_failed_send_is_retried(self, tmp_path):
        manager, sent = make_manager(tmp_path, FakePlayer(), ok=False)
        manager.log.add({"ad_id": "xparo-a", "status": "completed", "play_started_at": "x"})
        assert manager.upload_once() == 0
        assert len(manager.log.due_for_upload()) == 1

    def test_schedule_update_downloads_saves_and_stops_removed_ads(self, tmp_path):
        player = FakePlayer()
        manager, _ = make_manager(tmp_path, player)
        with patch.object(manager.store, "fetch", return_value=str(tmp_path / "x.png")):
            manager.update_schedule({"ads": [entry("xparo-a", type="image", media_url="https://x/a.png")]}, wait=True)
        assert manager.items[0]["path"].endswith("x.png")
        assert manager.store.load()["ads"][0]["id"] == "xparo-a"
        assert player.stopped_missing[-1] == {"xparo-a"}

    def test_delegating_backend_gets_the_schedule_and_reports_plays(self, tmp_path):
        delegate = FakeDelegate()
        manager, _ = make_manager(tmp_path, delegate)
        manager.update_schedule({"ads": [entry("xparo-a")]}, wait=True)
        assert delegate.schedules[-1][0]["id"] == "xparo-a"
        manager.tick(MON_10AM)                                # XP-shell runs the schedule, not us
        assert manager.log.recent() == []
        manager.record_external_play({"ad_id": "xparo-a", "status": "dismissed", "placement": "left",
                                      "play_started_at": "2026-10-05T10:00:00", "duration_played_sec": 3})
        manager.record_external_play({"status": "completed"})  # malformed: ignored
        assert [r["status"] for r in manager.log.recent()] == ["dismissed"]


# --------------------------------------------------------------------- native
class TestNative:
    def test_video_goes_exactly_over_its_box(self):
        cmd = ffplay_command("/ads/a.mp4", "video", (10, 20, 640, 360), 15, mute=True, ffplay="/usr/bin/ffplay")
        assert cmd[0] == "/usr/bin/ffplay" and cmd[-1] == "/ads/a.mp4"
        for flag, value in (("-left", "10"), ("-top", "20"), ("-x", "640"), ("-y", "360"), ("-t", "15")):
            assert cmd[cmd.index(flag) + 1] == value
        assert {"-noborder", "-alwaysontop", "-autoexit", "-an"} <= set(cmd)

    def test_voice_has_no_window(self):
        cmd = ffplay_command("/ads/a.mp3", "audio", (0, 0, 1, 1), 20)
        assert "-nodisp" in cmd and "-left" not in cmd

    def test_probe_size(self):
        run = MagicMock(return_value=MagicMock(stdout="1920x1080\n"))
        assert probe_size("/a.mp4", run=run) == (1920, 1080)
        assert probe_size("/a.mp4", run=MagicMock(side_effect=OSError)) is None

    def test_text_size_stays_in_bounds(self):
        assert 14 <= text_font_size("x" * 500, (0, 0, 200, 50)) <= 120
        assert text_font_size("Hi", (0, 0, 1920, 1080)) == 120

    def test_without_a_display_nothing_plays(self):
        backend = NativeBackend(env={})
        backend.start(MagicMock())
        assert backend.available is False
        done = MagicMock()
        backend.play(entry(), "bottom", done)
        done.assert_called_once_with("error", "no display")

    def test_backend_choice(self):
        assert make_backend("off").name == "off"
        assert make_backend("xpshell", node=None).name == "off"   # needs the ROS node
        assert make_backend("nonsense").name == "off"
        assert make_backend("native").name == "native"


# ----------------------------------------------------------------------- ros2
def test_xpshell_only_gets_xparo_ads():
    data = json.loads(schedule_message([entry("xparo-a", path="/ads/a.png"), entry("local-1")]))
    assert [a["id"] for a in data["ads"]] == ["xparo-a"]
    assert data["ads"][0]["path"] == "/ads/a.png"


# --------------------------------------------------------------------- engine
def test_engine_routes_ads_messages():
    from xparo.engine import Engine
    engine = Engine("secret", "proj-ads-test", connection_type="offline")
    engine.ad_manager = MagicMock()
    engine.on_ws_message(None, json.dumps({"ads_schedule": {"ads": []}, "ads_plays_ack": {"ack": [1]}}))
    engine.ad_manager.update_schedule.assert_called_once_with({"ads": []})
    engine.ad_manager.on_ack.assert_called_once_with({"ack": [1]})
    with patch.object(engine.local_database, "dashboard_receive"):
        engine.send_initial_data()
    engine.ad_manager.on_connected.assert_called_once()


def test_engine_setup_ads_off(tmp_path):
    from xparo.engine import Engine
    engine = Engine("secret", "proj-ads-test", connection_type="offline")
    engine.tmp_folder = str(tmp_path)
    manager = engine.setup_ads("off")
    try:
        assert manager.backend.name == "off"
        assert os.path.isdir(os.path.join(str(tmp_path), "xparo", "proj-ads-test", "ads"))
    finally:
        manager.stop()
