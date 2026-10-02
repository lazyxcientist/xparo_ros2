"""The robot's copy of its ad schedule and ad files, on disk, so ads keep
playing through a network outage or a restart."""
import hashlib
import json
import os
import urllib.parse
import urllib.request

MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mp3", ".ogg", ".wav", ".m4a"}
DOWNLOAD_TIMEOUT = 60
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024


class ScheduleStore:
    def __init__(self, folder, base_url=None):
        self.folder = folder
        self.media_dir = os.path.join(folder, "media")
        self.schedule_path = os.path.join(folder, "schedule.json")
        self.base_url = base_url  # callable -> "https://xparo.in" (media URLs may be site-relative)
        os.makedirs(self.media_dir, exist_ok=True)

    # ---------------------------------------------------------- schedule
    def load(self):
        try:
            with open(self.schedule_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {"ads": []}
        except (OSError, ValueError):
            return {"ads": []}

    def save(self, schedule):
        tmp = self.schedule_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(schedule, f, indent=2)
        os.replace(tmp, self.schedule_path)

    # ------------------------------------------------------------- media
    def absolute_url(self, url):
        if url.startswith("/") and self.base_url:
            base = self.base_url() if callable(self.base_url) else self.base_url
            return (base or "").rstrip("/") + url
        return url

    def media_path(self, url):
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
        if ext not in MEDIA_EXTS:
            ext = ".bin"
        return os.path.join(self.media_dir, hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ext)

    def fetch(self, url, opener=urllib.request.urlopen):
        """Download once; returns the local path. Raises OSError on failure."""
        url = self.absolute_url(url)
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise OSError(f"not a downloadable URL: {url}")
        path = self.media_path(url)
        if os.path.exists(path):
            return path
        tmp = path + ".part"
        with opener(url, timeout=DOWNLOAD_TIMEOUT) as response, open(tmp, "wb") as f:
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise OSError("ad file too large")
                f.write(chunk)
        os.replace(tmp, path)
        return path

    def localize(self, schedule, opener=urllib.request.urlopen):
        """Schedule entries with a local `path` for their file. Entries
        whose file can't be downloaded yet are left out (tried again on the
        next update) rather than played broken."""
        ready, missing = [], []
        for entry in schedule.get("ads") or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            item = {k: v for k, v in entry.items() if k != "media_url"}
            if entry.get("type") in ("image", "video", "audio"):
                url = entry.get("media_url")
                try:
                    if not url:
                        raise OSError("no media_url")
                    item["path"] = self.fetch(url, opener=opener)
                except (OSError, ValueError) as exc:
                    missing.append({"id": entry.get("id"), "error": str(exc)})
                    continue
            ready.append(item)
        return {"generated_at": schedule.get("generated_at"), "ads": ready}, missing

    def prune(self, schedule):
        keep = {os.path.abspath(e["path"]) for e in schedule.get("ads") or [] if e.get("path")}
        for name in os.listdir(self.media_dir):
            full = os.path.abspath(os.path.join(self.media_dir, name))
            if full not in keep and not name.endswith(".part"):
                try:
                    os.remove(full)
                except OSError:
                    pass
