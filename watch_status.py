"""Observability for the screen watcher (11 Oct 2026).

Frames live only in the program's RAM, so from outside nobody could tell whether the watcher, the screen text
reader and the live commentator were really working. Two things fix that:

(A) state\\watch_status.json, rewritten atomically every watch_status_interval_s by a daemon thread. Numbers and
    short fixed text only: no image data, no window titles, no screen text. `python watch_status_check.py` reads it.
(B) the voice command "save a frame" / "save the screen" / "show me what the watcher sees" writes the newest ring
    frame of each monitor as a JPEG into the shots folder. Only on that explicit command.
"""
import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATUS_PATH = BASE / "state" / "watch_status.json"
SHOTS_DIR = Path(__file__).resolve().parent / "state" / "shots"     # only written on the spoken "save a frame" command

DEFAULTS = {"watch_status_enabled": True, "watch_status_interval_s": 10}


def cfg_get(cfg, key):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


def monitor_labels(rects):
    """{mss index: 'left'|'middle'|'right'|'only'} by horizontal position (Windows layout)."""
    known = {m: r for m, r in (rects or {}).items() if r}
    order = sorted(known, key=lambda m: (known[m][0], known[m][1]))
    if len(order) == 1:
        return {order[0]: "only"}
    out = {}
    for i, m in enumerate(order):
        out[m] = "left" if i == 0 else "right" if i == len(order) - 1 else ("middle" if len(order) == 3 else f"middle {i}")
    return out


def _age(now, ts):
    return None if ts is None else round(max(0.0, now - ts), 1)


def snapshot(watch=None, text=None, commentary=None, clock=time.monotonic, wall=time.time, prev=None):
    """Status dict. Never raises: a broken part reports {"error": <exception class>}."""
    now = clock()
    out = {"t": round(wall(), 2), "written": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if watch is None:
            out["watch"] = {"present": False}
        else:
            st = watch.status()
            rects = watch.monitor_rects()
            labels = monitor_labels(rects)
            ring = watch.ring.stats() if hasattr(watch.ring, "stats") else {}
            per = getattr(watch, "per_monitor", {}) or {}
            using = getattr(getattr(watch, "_backend", None), "using", {}) or {}
            prev_mons = ((prev or {}).get("watch") or {}).get("monitors") or {}
            mons = {}
            for m in sorted(set(rects) | set(per) | set(ring)):
                pm, rs = per.get(m, {}), ring.get(m, {})
                errs = int(pm.get("errors", 0))
                before = (prev_mons.get(str(m)) or {}).get("errors")
                mons[str(m)] = {
                    "mss_index": m, "label": labels.get(m, "?"),
                    "rect": list(rects[m]) if rects.get(m) else None,
                    "backend": (using.get(m, "mss") if getattr(watch, "_backend", None) is not None else "none"),
                    "grabs": int(pm.get("grabs", 0)), "errors": errs,
                    "errors_since_last": None if before is None else max(0, errs - int(before)),
                    "last_error": str(pm.get("last_error", ""))[:60],
                    "newest_frame_age_s": _age(now, rs.get("newest_ts")),
                    "ring_frames": int(rs.get("frames", 0)), "ring_bytes": int(rs.get("bytes", 0)),
                }
            reason = getattr(watch, "_paused_reason", "") or getattr(watch, "_skip_reason", "")
            out["watch"] = {
                "present": True, "running": bool(watch.running), "watching": bool(watch.watching),
                "state": st.get("state"), "paused": bool(reason), "paused_reason": str(reason)[:40],
                "interval_s": st.get("interval_s"), "ticks": st.get("ticks", 0), "grabs": st.get("grabs", 0),
                "events": st.get("events", 0), "errors": st.get("errors", 0),
                "moving_keeps": st.get("moving_keeps", 0), "monitor_changes": st.get("monitor_changes", 0),
                "ring_frames": st.get("ring_frames", 0), "ring_bytes": st.get("ring_bytes", 0),
                "monitors": mons,
            }
    except Exception as e:
        out["watch"] = {"present": True, "error": type(e).__name__}
    try:
        if text is None:
            out["screen_text"] = {"present": False}
        else:
            c = text.counters
            out["screen_text"] = {
                "present": True, "running": bool(text.running), "passes": int(c.get("passes", 0)),
                "failures": int(c.get("errors", 0)), "hints": int(c.get("hints", 0)), "ocr": int(c.get("ocr", 0)),
                "window_ocr": int(c.get("window_ocr", 0)), "window_uia": int(c.get("window_uia", 0)),
                "skipped_paused": int(c.get("skipped_paused", 0)), "skipped_cpu": int(c.get("skipped_cpu", 0)),
                "last_pass_age_s": _age(now, getattr(text, "last_pass_ts", None)),
            }
    except Exception as e:
        out["screen_text"] = {"present": True, "error": type(e).__name__}
    try:
        if commentary is None:
            out["commentary"] = {"present": False}
        else:
            c = commentary.counters
            out["commentary"] = {
                "present": True, "enabled": bool(commentary.enabled),
                "running": bool(getattr(commentary, "_thread", None) is not None
                                and commentary._thread.is_alive()),
                "looks": c.get("looks", 0), "lines": c.get("lines", 0), "silent": c.get("silent", 0),
                "deaths": int(c.get("deaths", 0)),
                "busy": c.get("skipped_busy", 0), "cap": c.get("skipped_cap", 0), "errors": c.get("errors", 0),
                "last_decision": str(getattr(commentary, "last_reason", ""))[:30],
                "last_look_age_s": _age(commentary.clock(), getattr(commentary, "last_look", None)),
            }
    except Exception as e:
        out["commentary"] = {"present": True, "error": type(e).__name__}
    return out


def write_atomic(data, path=STATUS_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def start(app, cfg, path=STATUS_PATH):
    """Daemon thread rewriting the status file. Returns the thread, or None when off/failed."""
    if not cfg_get(cfg, "watch_status_enabled"):
        return None
    every = max(1.0, float(cfg_get(cfg, "watch_status_interval_s")))

    def loop():
        prev = None
        while True:
            try:
                d = app.__dict__
                prev = snapshot(d.get("screen_watch"), d.get("screen_text"), d.get("commentary"), prev=prev)
                write_atomic(prev, path)
            except Exception:
                pass                                   # a status file must never hurt the app
            time.sleep(every)
    try:
        t = threading.Thread(target=loop, daemon=True, name="watch-status")
        t.start()
        return t
    except Exception:
        return None


# ------------------------------------------------------------------ (B) save a frame
_SAVE_RE = re.compile(r"(?:ok |okay |hey aida,? |aida,? )?(?:please )?(?:"
                      r"save (?:a|the|one) (?:frame|screen)|save the screens|save a screenshot of the watcher|"
                      r"show me what (?:the )?watcher (?:sees|can see|is seeing))(?: please)?\W*")


def parse_save_command(t):
    return bool(_SAVE_RE.fullmatch((t or "").lower().strip()))


def save_frames(watch, folder=SHOTS_DIR, clock=time.monotonic, now=None):
    """Write the newest ring frame of each monitor as JPEG. Returns [(path, mon, label, age_s)]."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    rects = watch.monitor_rects()
    labels = monitor_labels(rects)
    mons = sorted(set(rects) | set(watch.ring.stats() if hasattr(watch.ring, "stats") else {}))
    saved = []
    for m in mons:
        f = watch.get_latest_frame(m)
        if not f or not f.get("jpeg"):
            continue
        p = folder / f"watcher-{stamp}-mon{m}.jpg"
        p.write_bytes(f["jpeg"])
        saved.append((p, m, labels.get(m, "?"), round(max(0.0, clock() - f["ts"]), 1)))
    return saved


def save_reply(saved, folder=SHOTS_DIR):
    if not saved:
        return "Nothing to save. The watcher has no frames in memory right now."
    parts = ", ".join(f"{lab} {int(age)} seconds old" for _p, _m, lab, age in saved)
    n = len(saved)
    return f"Saved {n} frame{'s' if n != 1 else ''} to the shots folder in state: {parts}."
