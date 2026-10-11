"""Screenshots of the robot's own screen, taken while an ad plays (proof of
play for the advertiser). Never the camera.

Pillow's ImageGrab reads the X11 display (and, on Wayland, uses
gnome-screenshot / grim / spectacle when one is installed); ffmpeg's x11grab
is the fallback. Each capture is shrunk to MAX_WIDTH and saved as a JPEG
small enough to send over the robot's connection.
"""
import io
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("xparo.ads")

MAX_WIDTH = 640
QUALITY = 70
MAX_BYTES = 350 * 1024


def _grab_pillow():
    from PIL import ImageGrab
    return ImageGrab.grab()


def _grab_ffmpeg():
    display = os.environ.get("DISPLAY")
    ffmpeg = shutil.which("ffmpeg")
    if not display or not ffmpeg:
        return None
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "screen.png")
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "x11grab", "-i", display, "-frames:v", "1", out],
                       capture_output=True, timeout=10, check=True)
        with Image.open(out) as image:
            return image.copy()


def jpeg_bytes(image, max_width=MAX_WIDTH, quality=QUALITY):
    image = image.convert("RGB")
    if image.width > max_width:
        image = image.resize((max_width, max(1, round(image.height * max_width / image.width))))
    for q in (quality, 55, 40):
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue()
    return buf.getvalue()


class ScreenCapture:
    """grab(path) -> True if a JPEG of the whole screen was written to path."""

    def __init__(self, grabbers=None):
        self.grabbers = grabbers or (_grab_pillow, _grab_ffmpeg)
        self._available = None

    @property
    def available(self):
        """Checked once with a real capture (and again after a later success)."""
        if self._available is None:
            self._available = self._grab() is not None
        return self._available

    def _grab(self):
        for grabber in self.grabbers:
            try:
                image = grabber()
            except Exception as exc:  # no display, no tool, a Wayland compositor that refuses...
                log.debug("screen capture with %s failed: %s", getattr(grabber, "__name__", grabber), exc)
                continue
            if image is not None and image.width and image.height:
                return image
        return None

    def grab(self, path):
        image = self._grab()
        if image is None:
            self._available = False
            return False
        data = jpeg_bytes(image)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self._available = True
        return True
