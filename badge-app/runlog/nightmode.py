# ---------------------------------------------------------------------------
# Night mode + clock helpers.
#
# Pure Python (no badge/hardware imports) so the logic can be unit-tested under
# CPython and reused unchanged on the badge (MicroPython).
#
# The badge has no reliable always-on wall clock, so we derive the local time
# from a periodic network "sync" (Open-Meteo returns local time when the request
# uses timezone=auto) plus io.ticks (ms since boot) to advance it between syncs.
# ---------------------------------------------------------------------------


def parse_local_hm(iso):
    """Parse the hour/minute out of an ISO-ish local timestamp.

    Accepts strings like "2024-06-10T23:05" or "2024-06-10T23:05:00".
    Returns (hour, minute) or None if it can't be parsed.
    """
    try:
        time_part = iso.split("T", 1)[1]
        bits = time_part.split(":")
        h = int(bits[0])
        m = int(bits[1])
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return None


def mins_now(sync_min, sync_ticks, now_ticks):
    """Current minutes-since-midnight, advanced from the last sync.

    sync_min   minutes-of-day captured at the last sync (0..1439)
    sync_ticks io.ticks (ms) captured at the last sync
    now_ticks  io.ticks (ms) now
    Returns 0..1439, or None if we have never synced.
    """
    if sync_min is None or sync_ticks is None:
        return None
    elapsed_min = (now_ticks - sync_ticks) // 60000
    return (sync_min + elapsed_min) % 1440


def in_window(mins, start_h, end_h):
    """True if minutes-of-day falls in [start_h:00, end_h:00).

    Handles windows that wrap past midnight (e.g. 23:00 -> 06:00).
    """
    if mins is None:
        return False
    start = (start_h % 24) * 60
    end = (end_h % 24) * 60
    if start == end:
        return False
    if start < end:
        return start <= mins < end
    # window wraps midnight
    return mins >= start or mins < end


class NightMode:
    """Tracks local time and decides when the display should be dark.

    Usage on the badge:
        night = NightMode(start_h=23, end_h=6, wake_ms=20000)
        # after a weather fetch that used timezone=auto:
        night.sync_from_iso(current["time"], io.ticks)
        # each frame:
        if any_button_pressed:
            night.wake(io.ticks)
        if night.should_sleep(io.ticks):
            # turn the display off
    """

    def __init__(self, start_h=23, end_h=6, wake_ms=20000):
        self.start_h = start_h
        self.end_h = end_h
        self.wake_ms = wake_ms
        self.sync_min = None
        self.sync_ticks = None
        self.awake_until = None

    def sync_from_hm(self, h, m, now_ticks):
        self.sync_min = (h % 24) * 60 + (m % 60)
        self.sync_ticks = now_ticks

    def sync_from_iso(self, iso, now_ticks):
        hm = parse_local_hm(iso)
        if hm is not None:
            self.sync_from_hm(hm[0], hm[1], now_ticks)
            return True
        return False

    def has_time(self):
        return self.sync_min is not None

    def current_min(self, now_ticks):
        return mins_now(self.sync_min, self.sync_ticks, now_ticks)

    def hhmm(self, now_ticks):
        """Return "HH:MM" for the current derived time, or "--:--"."""
        m = self.current_min(now_ticks)
        if m is None:
            return "--:--"
        return "%02d:%02d" % (m // 60, m % 60)

    def is_night(self, now_ticks):
        return in_window(self.current_min(now_ticks), self.start_h, self.end_h)

    def wake(self, now_ticks):
        """Register a button press: keep the screen on for wake_ms."""
        self.awake_until = now_ticks + self.wake_ms

    def is_wake_override(self, now_ticks):
        return self.awake_until is not None and now_ticks < self.awake_until

    def should_sleep(self, now_ticks):
        """Display should be OFF: it's night and no recent button press."""
        return self.is_night(now_ticks) and not self.is_wake_override(now_ticks)
