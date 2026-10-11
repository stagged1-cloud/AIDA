"""Tiny status heartbeat so an external restart can wait until the assistant has finished talking.

Bug (10 Oct 2026): restart_app.ps1 waited a fixed 12 s, so it sometimes killed the app while AIDA was still
speaking its own reply, before the user had heard or read it. The app now writes state\\app_status.json about twice a
second: {"t": epoch seconds, "working": bool, "speaking": bool}. restart_app.ps1 waits until the status has been
idle (not working and not speaking) for a few seconds in a row, or until the file goes stale (app dead or hung).
"""
import json
import os
import threading
import time
from pathlib import Path

STATUS_PATH = Path(__file__).parent / "state" / "app_status.json"


def snapshot(app, clock=time.time):
    """The status dict for `app` (never raises: a broken attribute counts as idle)."""
    def flag(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return {"t": round(clock(), 2),
            "working": flag(lambda: app.working),
            "speaking": flag(lambda: app.mouth.speaking)}


def write_atomic(data, path=STATUS_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def start(app, path=STATUS_PATH, every=0.5):
    """Start the daemon heartbeat thread. Returns the thread (or None if it could not start)."""
    def loop():
        while True:
            try:
                write_atomic(snapshot(app), path)
            except Exception:
                pass                                   # a status file must never hurt the app
            time.sleep(every)
    try:
        t = threading.Thread(target=loop, daemon=True, name="app-status")
        t.start()
        return t
    except Exception:
        return None
