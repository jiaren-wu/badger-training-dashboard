import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "runlog"))
import nightmode as nm

fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)

# --- parse_local_hm ---
check("parse basic", nm.parse_local_hm("2024-06-10T23:05") == (23, 5))
check("parse seconds", nm.parse_local_hm("2024-06-10T06:00:00") == (6, 0))
check("parse midnight", nm.parse_local_hm("2024-01-01T00:00") == (0, 0))
check("parse junk", nm.parse_local_hm("nope") is None)
check("parse none", nm.parse_local_hm("") is None)

# --- mins_now: advances by io.ticks ---
# synced at 23:00 (1380) at tick 10_000; 90 min later
check("mins advance", nm.mins_now(1380, 10_000, 10_000 + 90*60_000) == (1380 + 90) % 1440)
check("mins wrap midnight", nm.mins_now(1380, 0, 120*60_000) == (1380 + 120) % 1440)  # ->60 (01:00)
check("mins none", nm.mins_now(None, 0, 0) is None)

# --- in_window: night 23:00-06:00 wraps midnight ---
check("win 23:00 night", nm.in_window(23*60, 23, 6) is True)
check("win 23:30 night", nm.in_window(23*60+30, 23, 6) is True)
check("win 02:00 night", nm.in_window(2*60, 23, 6) is True)
check("win 05:59 night", nm.in_window(5*60+59, 23, 6) is True)
check("win 06:00 day", nm.in_window(6*60, 23, 6) is False)
check("win 22:59 day", nm.in_window(22*60+59, 23, 6) is False)
check("win 12:00 day", nm.in_window(12*60, 23, 6) is False)
check("win none", nm.in_window(None, 23, 6) is False)
# non-wrapping window (sanity)
check("win daytime 09-17 at 12", nm.in_window(12*60, 9, 17) is True)
check("win daytime 09-17 at 08", nm.in_window(8*60, 9, 17) is False)

# --- NightMode controller ---
n = nm.NightMode(start_h=23, end_h=6, wake_ms=20_000)
check("no time -> not night", n.is_night(0) is False)
check("no time -> not sleep", n.should_sleep(0) is False)

# sync to 23:30 at tick 100_000
n.sync_from_iso("2024-06-10T23:30", 100_000)
check("has time", n.has_time() is True)
check("hhmm", n.hhmm(100_000) == "23:30")
check("is night at sync", n.is_night(100_000) is True)
check("should sleep at night", n.should_sleep(100_000) is True)

# press a button -> wake override keeps it on for 20s
n.wake(100_000)
check("awake overrides sleep", n.should_sleep(105_000) is False)
check("still night underneath", n.is_night(105_000) is True)
# after wake window elapses -> sleep again
check("sleeps after wake window", n.should_sleep(100_000 + 21_000) is True)

# advance derived clock to 06:15 (day) -> never sleep even without wake
# from 23:30 to 06:15 next day = 6h45m = 405 min
later = 100_000 + 405*60_000
check("hhmm morning", n.hhmm(later) == "06:15")
check("not night in morning", n.is_night(later) is False)
check("not sleep in morning", n.should_sleep(later) is False)

print()
print("TOTAL FAILURES:", len(fails), fails)
sys.exit(1 if fails else 0)
