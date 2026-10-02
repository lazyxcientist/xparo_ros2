"""Which ads are due right now -- the same rules as XP-shell's
services/ads/scheduler.py (days, daily times, valid dates), so a schedule
plays identically on either player."""
import datetime

DAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PLACEMENTS = ("fullscreen", "left", "right", "top", "bottom", "center")
DEFAULT_PLACEMENT = "fullscreen"


def _time(value, default):
    try:
        hours, minutes = str(value).split(":")[:2]
        return datetime.time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return default


def _date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def is_active(item, now=None):
    """True if `item` (one schedule entry) should be playing at `now`."""
    now = now or datetime.datetime.now()
    if not item.get("enabled", True):
        return False
    today = now.date()
    valid_from, valid_until = _date(item.get("valid_from")), _date(item.get("valid_until"))
    if (valid_from and today < valid_from) or (valid_until and today > valid_until):
        return False
    days = item.get("days", "all")
    if days not in ("all", None, []) and DAY_ABBR[today.weekday()] not in days:
        return False
    start = _time(item.get("start_time"), datetime.time(0, 0))
    end = _time(item.get("end_time"), datetime.time(23, 59))
    current = now.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end  # runs past midnight


def placement_of(item):
    placement = item.get("placement") or item.get("slot") or DEFAULT_PLACEMENT
    return placement if placement in PLACEMENTS else "center"


def active_by_placement(items, now=None):
    """{placement: [items due now, lowest priority number first]}"""
    groups = {}
    for item in items:
        if is_active(item, now):
            groups.setdefault(placement_of(item), []).append(item)
    for queue in groups.values():
        queue.sort(key=lambda i: (i.get("priority", 99), str(i.get("id"))))
    return groups
