"""Which robot this machine is, kept stable across reboots and hardware
tweaks, and different on a cloned SD card.

The id used to be derived from uuid.getnode() on every boot: swapping a USB
Wi-Fi dongle (or MAC randomisation) changed it, and the dashboard grew a
duplicate robot. Meanwhile a cloned SD card carried the original robot's
saved credential, so two physical robots shared one dashboard robot and
commands/teleop went to whichever had connected last.

Now the first id is saved together with every network interface's MAC.
Later boots keep the saved id while at least one of those MACs is still
present (same machine, maybe a new dongle). If none is, the card is running
on other hardware: a new id is made and the copied credential is dropped,
so this machine registers as its own robot.
"""
import json
import os

import psutil

from .fileutil import atomic_write_json

IDENTITY_FILE = "robot_identity.json"


def current_macs():
    macs = set()
    try:
        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                mac = (address.address or "").lower()
                if address.family == psutil.AF_LINK and mac and mac != "00:00:00:00:00:00":
                    macs.add(mac)
    except Exception:
        pass
    return macs


def resolve(state_dir, compute_id, macs=None):
    """Returns (device_id, moved_to_other_hardware)."""
    macs = current_macs() if macs is None else set(macs)
    path = os.path.join(state_dir, IDENTITY_FILE)
    saved = None
    try:
        with open(path) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        pass
    if isinstance(saved, dict) and saved.get("device_id"):
        saved_macs = set(saved.get("macs") or [])
        if not saved_macs or not macs or saved_macs & macs:
            if macs - saved_macs:
                _save(path, saved["device_id"], saved_macs | macs)   # remember a new dongle
            return saved["device_id"], False
        device_id = compute_id()
        _save(path, device_id, macs)
        return device_id, True
    device_id = compute_id()
    _save(path, device_id, macs)
    return device_id, False


def _save(path, device_id, macs):
    try:
        atomic_write_json(path, {"device_id": device_id, "macs": sorted(macs)})
    except OSError as e:
        print(f"[xparo] couldn't save the robot identity ({e})")
