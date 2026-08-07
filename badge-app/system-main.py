# This file is copied from /system/main.py to /main.py on first run.
#
# Customized for Jiaren's badge: boot straight into the "runlog" training
# dashboard and run it 24/7. Any firmware reset (watchdog, power blip, HOME
# press) self-heals right back into training instead of the launcher.
#
# Escape hatch: hold any face button (A / B / C / UP / DOWN) at power-on to
# reach the normal app menu. If runlog is missing or fails to import, we also
# fall back to the menu automatically (never a reset loop).
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


# Decide what to launch: training by default, menu on request/fallback.
if _boot_escape() or not _exists(AUTO_APP):
    app = run_menu()
else:
    app = AUTO_APP

# Guard against a bad menu return so we never brick the boot.
if not (isinstance(app, str) and _exists(app)):
    app = AUTO_APP

# Don't pass the button press into the app
while io.held:
    io.poll()

machine.Pin.board.BUTTON_HOME.irq(
    trigger=machine.Pin.IRQ_FALLING, handler=quit_to_launcher
)

sys.path.insert(0, app)
os.chdir(app)

running_app = __import__(app)

getattr(running_app, "init", lambda: None)()

run(running_app.update)

# If the app ever returns (HOME) or crashes, reset -> auto-launch training.
machine.reset()
