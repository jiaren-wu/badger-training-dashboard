# This file is copied from /system/main.py to /main.py on first run.
#
# Customized for Jiaren's badge: boot straight into the "runlog" training
# dashboard and run it 24/7. Any firmware reset (watchdog, power blip, HOME
# press) self-heals right back into training instead of the launcher.
#
# Escape hatch: hold any face button (A / B / C / UP / DOWN) at power-on to
# reach the normal app menu. If the live runlog build fails to import, we fall
# back to the known-good stable build, then to the menu -- never a reset loop.
#
# To fully restore stock behaviour, copy main.py.orig back over this file in
# disk mode.

import sys
import os
from badgeware import run, io
import machine
import gc
import powman

AUTO_APP = "/system/apps/runlog"
STABLE_APP = "/system/apps/runlog_stable"

running_app = None


def quit_to_launcher(pin):
    global running_app
    getattr(running_app, "on_exit", lambda: None)()
    # If we reset while boot is low, bad times
    while not pin.value():
        pass
    machine.reset()


def _boot_escape():
    # Hold any face button at power-on to reach the full menu.
    try:
        for _ in range(5):
            io.poll()
        return bool(io.held)
    except Exception:
        return False


def _exists(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False


def run_menu():
    try:
        skip_cinematic = powman.get_wake_reason() == powman.WAKE_WATCHDOG
    except Exception:
        skip_cinematic = False
    if not skip_cinematic:
        try:
            startup = __import__("/system/apps/startup")
            run(startup.update)
            if sys.path and sys.path[0].startswith("/system/apps"):
                sys.path.pop(0)
            del startup
            gc.collect()
        except Exception as e:
            print("startup error:", e)
    menu = __import__("/system/apps/menu")
    chosen = run(menu.update)
    if sys.path and sys.path[0].startswith("/system/apps"):
        sys.path.pop(0)
    try:
        del menu
    except Exception:
        pass
    for _m in ("ui", "icon"):
        if _m in sys.modules:
            del sys.modules[_m]
    gc.collect()
    return chosen


# Decide what to launch: training by default, menu on explicit request.
if _boot_escape():
    chosen = run_menu()
    candidates = [chosen] if (isinstance(chosen, str) and _exists(chosen)) else [AUTO_APP]
else:
    # Auto-boot the training dashboard. Try the live build first; if it fails
    # to import/init (corrupt copy, compile-time MemoryError, brush/pen limit,
    # a bug in init()), fall back to the known-good stable build so the badge
    # still comes up as a working dashboard. Only if BOTH fail do we drop to
    # the launcher menu -- never a blank REPL.
    candidates = [AUTO_APP, STABLE_APP]

# Don't pass the button press into the app.
while io.held:
    io.poll()

machine.Pin.board.BUTTON_HOME.irq(
    trigger=machine.Pin.IRQ_FALLING, handler=quit_to_launcher
)

# Reclaim and de-fragment the heap before the big (~90 KB) app import. Boot-time
# free memory is razor-thin; a fresh collect gives the app the best chance to
# fit. The printed value shows up on the serial console for diagnostics.
gc.collect()
try:
    print("boot: free heap =", gc.mem_free(),
          "mpy =", getattr(sys.implementation, "_mpy", "?"),
          "ver =", sys.implementation.version)
except Exception:
    pass

booted = False
launched = False
for candidate in candidates:
    if not _exists(candidate):
        continue
    pre_mods = set(sys.modules.keys())
    try:
        sys.path.insert(0, candidate)
        os.chdir(candidate)
        running_app = __import__(candidate)
        getattr(running_app, "init", lambda: None)()
        launched = True
        run(running_app.update)  # blocks until the app exits or crashes
        booted = True
        break
    except Exception as e:
        try:
            print("boot: app failed:", candidate, "->", e)
            sys.print_exception(e)
        except Exception:
            pass
        gc.collect()
        if launched:
            # Import + init succeeded but the running app crashed at runtime.
            # Don't swap in a different build mid-stream; fall through to menu.
            break
        # Import/init failed: roll back sys.path / modules / cwd so the next
        # candidate (or the menu) imports from a clean state.
        try:
            if sys.path and sys.path[0] == candidate:
                sys.path.pop(0)
            for _m in list(sys.modules.keys()):
                if _m not in pre_mods:
                    del sys.modules[_m]
            os.chdir("/")
        except Exception:
            pass
        running_app = None
        continue

if not booted:
    try:
        run_menu()
    except Exception:
        pass

# If we get here, reset -> firmware auto-launches training again.
machine.reset()
