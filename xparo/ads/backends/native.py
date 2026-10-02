"""xparo's own ad player, for robots that don't run XP-shell.

Every ad is a borderless, always-on-top window placed with geometry.ad_box
(so it keeps its own shape), re-raised every second so other windows can't
cover it. Photos and text are drawn by Tk (Pillow for images); video and
voice are played by ffplay (from FFmpeg), with the video window laid
exactly over the ad's box. Like a web ad, each one carries a small "Ad"
label and a close (X) button -- shown after the listing's close delay --
and closing it ends that play as "dismissed".

Needs a desktop session (DISPLAY or WAYLAND_DISPLAY), python3-tk, Pillow,
and ffmpeg for video/voice. Without a display the backend reports itself
unavailable and no ads are started (nothing is logged as failed).

Tk is not thread-safe, so the whole UI runs on one thread; play()/stop
requests arrive through a queue that thread drains.
"""
import logging
import os
import queue
import shutil
import subprocess
import threading

from ..geometry import ad_box
from .base import DisplayBackend

log = logging.getLogger("xparo.ads")

DRAIN_MS = 50
RAISE_MS = 1000
POLL_MS = 250
PLAYER_GRACE_SECONDS = 10  # ffplay's own -t should end it; this catches a hung player
BADGE_SIZE = (86, 30)


def ffplay_command(path, media_type, box, duration, mute=False, ffplay="ffplay"):
    """The ffplay command line for a video (placed over `box`) or a voice ad (no window)."""
    cmd = [ffplay, "-hide_banner", "-loglevel", "error", "-autoexit", "-t", f"{float(duration):g}"]
    if media_type == "audio":
        cmd.append("-nodisp")
    else:
        x, y, w, h = box
        cmd += ["-noborder", "-alwaysontop", "-left", str(x), "-top", str(y), "-x", str(w), "-y", str(h)]
        if mute:
            cmd.append("-an")
    cmd.append(path)
    return cmd


def probe_size(path, run=subprocess.run, ffprobe="ffprobe"):
    """A video's (width, height), or None if ffprobe can't tell."""
    try:
        out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                   "-of", "csv=p=0:s=x", path], capture_output=True, text=True, timeout=10).stdout
        width, height = out.strip().splitlines()[0].split("x")[:2]
        return int(width), int(height)
    except Exception:
        return None


def text_font_size(text, box):
    """A font size that fits `text` inside the box, roughly."""
    _, _, w, h = box
    chars = max(len(text or ""), 1)
    by_height = h * 0.34
    by_width = (w * 1.6) / chars ** 0.75
    return int(max(14, min(by_height, by_width, 120)))


class _Play:
    """One ad on screen. finish() runs exactly once."""

    def __init__(self, backend, item, placement, on_finished):
        self.backend = backend
        self.item = item
        self.placement = placement
        self.on_finished = on_finished
        self.windows = []
        self.proc = None
        self.images = []  # keep Tk images alive
        self.timers = []
        self.done = False

    def after(self, ms, fn):
        self.timers.append(self.backend.root.after(int(ms), fn))

    def finish(self, status, error=None):
        if self.done:
            return
        self.done = True
        for timer in self.timers:
            try:
                self.backend.root.after_cancel(timer)
            except Exception:
                pass
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        for win in self.windows:
            try:
                win.destroy()
            except Exception:
                pass
        if self.backend.playing.get(self.placement) is self:
            del self.backend.playing[self.placement]
        try:
            self.on_finished(status, error)
        except Exception:
            log.exception("ad finish callback failed")


class NativeBackend(DisplayBackend):
    name = "native"

    def __init__(self, popen=subprocess.Popen, which=shutil.which, env=os.environ):
        self.popen = popen
        self.which = which
        self.env = env
        self.available = False
        self.commands = queue.Queue()
        self.playing = {}
        self.root = None
        self.screen = (1280, 720)
        self._ready = threading.Event()

    # ----------------------------------------------------------- lifecycle
    def start(self, manager):
        super().start(manager)
        if not (self.env.get("DISPLAY") or self.env.get("WAYLAND_DISPLAY")):
            log.warning("no display on this robot (DISPLAY/WAYLAND_DISPLAY unset); ads won't play")
            return
        threading.Thread(target=self._ui_main, name="xparo-ads-ui", daemon=True).start()
        self._ready.wait(10)
        if not self.available:
            log.warning("could not open the ad player window; ads won't play")

    def _ui_main(self):
        try:
            import tkinter
            self.tk = tkinter
            self.root = tkinter.Tk()
            self.root.withdraw()
            self.screen = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
            self.available = True
        except Exception as exc:
            log.warning("Tk unavailable: %s", exc)
            self._ready.set()
            return
        self._ready.set()
        self.root.after(DRAIN_MS, self._drain)
        self.root.after(RAISE_MS, self._keep_on_top)
        try:
            self.root.mainloop()
        finally:
            self.available = False

    def play(self, item, placement, on_finished):
        if not self.available:
            on_finished("error", "no display")
            return
        self.commands.put(("play", item, placement, on_finished))

    def stop_missing(self, live_ids):
        self.commands.put(("stop_missing", set(live_ids)))

    def stop_all(self):
        self.commands.put(("stop_all",))

    def close(self):
        self.commands.put(("quit",))

    # ----------------------------------------------------------- UI thread
    def _drain(self):
        try:
            while True:
                command = self.commands.get_nowait()
                kind = command[0]
                if kind == "play":
                    self._show(*command[1:])
                elif kind == "stop_missing":
                    for play in list(self.playing.values()):
                        if play.item.get("id") not in command[1]:
                            play.finish("interrupted", "taken off the schedule")
                elif kind in ("stop_all", "quit"):
                    for play in list(self.playing.values()):
                        play.finish("interrupted", "player stopped")
                    if kind == "quit":
                        self.root.quit()
                        return
        except queue.Empty:
            pass
        self.root.after(DRAIN_MS, self._drain)

    def _keep_on_top(self):
        """Force ads above everything else, every second."""
        for play in list(self.playing.values()):
            for win in play.windows:
                try:
                    win.attributes("-topmost", True)
                    win.lift()
                except Exception:
                    pass
        self.root.after(RAISE_MS, self._keep_on_top)

    def _window(self, box, bg="#000000"):
        x, y, w, h = box
        win = self.tk.Toplevel(self.root, bg=bg)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"{w}x{h}+{x}+{y}")
        return win

    def _show(self, item, placement, on_finished):
        previous = self.playing.get(placement)
        if previous:
            previous.finish("interrupted", "replaced by the next ad")
        play = _Play(self, item, placement, on_finished)
        self.playing[placement] = play
        try:
            self._build(play)
        except Exception as exc:
            log.exception("ad %s failed to show", item.get("id"))
            play.finish("error", str(exc)[:300])

    def _build(self, play):
        item = play.item
        media_type = item.get("type") or "image"
        duration = float(item.get("duration") or 15)
        path = item.get("path")
        screen_w, screen_h = self.screen
        if media_type in ("image", "video", "audio") and not (path and os.path.exists(path)):
            play.finish("error", "ad file missing")
            return

        if media_type == "image":
            from PIL import Image, ImageTk
            image = Image.open(path)
            image.load()
            box = ad_box(screen_w, screen_h, play.placement, image.size, "image")
            win = self._window(box)
            photo = ImageTk.PhotoImage(image.convert("RGB").resize((box[2], box[3])))
            play.images.append(photo)
            self.tk.Label(win, image=photo, bd=0, bg="#000000").pack(fill="both", expand=True)
            play.windows.append(win)
            play.after(duration * 1000, lambda: play.finish("completed"))
        elif media_type == "text":
            box = ad_box(screen_w, screen_h, play.placement, None, "text")
            win = self._window(box)
            text = item.get("text") or ""
            self.tk.Label(win, text=text, fg="#ffffff", bg="#000000", wraplength=max(box[2] - 40, 50),
                          justify="center", font=("Helvetica", text_font_size(text, box), "bold")
                          ).pack(fill="both", expand=True, padx=20, pady=10)
            play.windows.append(win)
            play.after(duration * 1000, lambda: play.finish("completed"))
        elif media_type in ("video", "audio"):
            ffplay = self.which("ffplay")
            if not ffplay:
                play.finish("error", "ffplay (FFmpeg) is not installed")
                return
            if media_type == "video":
                size = probe_size(path, ffprobe=self.which("ffprobe") or "ffprobe") or (16, 9)
                box = ad_box(screen_w, screen_h, play.placement, size, "video")
                play.windows.append(self._window(box))  # black backdrop until the video window appears
            else:
                box = ad_box(screen_w, screen_h, play.placement, None, "audio")
                win = self._window(box, bg="#111114")
                self.tk.Label(win, text="🔊  " + (item.get("title") or "Ad"), fg="#ffffff", bg="#111114",
                              font=("Helvetica", 16, "bold"), wraplength=box[2] - 30).pack(fill="both", expand=True, padx=14)
                play.windows.append(win)
            play.proc = self.popen(ffplay_command(path, media_type, box, duration, mute=item.get("mute", False),
                                                  ffplay=ffplay),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self._watch_player(play, duration)
        else:
            play.finish("error", f"unknown ad type {media_type!r}")
            return
        self._add_badge(play, box)

    def _watch_player(self, play, duration, elapsed_ms=0):
        if play.done:
            return
        code = play.proc.poll()
        if code is not None:
            if code == 0:
                play.finish("completed")
            else:
                error = (play.proc.stderr.read().decode("utf-8", "replace") if play.proc.stderr else "")[-300:]
                play.finish("error", error.strip() or f"player exited with code {code}")
            return
        if elapsed_ms > (duration + PLAYER_GRACE_SECONDS) * 1000:
            play.finish("completed")
            return
        play.after(POLL_MS, lambda: self._watch_player(play, duration, elapsed_ms + POLL_MS))

    def _add_badge(self, play, box):
        """The "Ad" label and close button, top-right of the ad, like a web ad."""
        x, y, w, _ = box
        bw, bh = BADGE_SIZE

        def show():
            if play.done:
                return
            badge = self._window((x + w - bw - 6, y + 6, bw, bh), bg="#202024")
            self.tk.Label(badge, text="Ad", fg="#e7e5e4", bg="#202024", font=("Helvetica", 11, "bold")).pack(side="left", padx=(8, 2))
            self.tk.Button(badge, text="✕", command=lambda: play.finish("dismissed"), fg="#ffffff", bg="#202024",
                           activebackground="#ff7722", bd=0, highlightthickness=0, font=("Helvetica", 12, "bold"),
                           cursor="hand2").pack(side="right", padx=(2, 6))
            play.windows.append(badge)

        delay = max(0, min(int(play.item.get("close_delay_sec") or 0), 30))
        if delay:
            play.after(delay * 1000, show)
        else:
            show()
