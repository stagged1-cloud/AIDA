"""Live assistant M3: local text tier, LOG-ONLY.

Reads the text in front of the user and keeps an in-RAM picture of the subject (context_watch.ContextTracker),
then compares what he is typing against it. Would-be hints are appended to logs/context_shadow.jsonl and
nothing else happens: no popup, no speech, no Claude, no network. Screen text and pixels never touch disk;
the only write is that log line (typed text and clashing entity capped at 60 characters).

Sources, per research/live_assistant/03_local_understanding.md:
- UI Automation first: the focused control. An edit control is what the user is typing (password boxes are
  skipped); a document/text control gives screen text without OCR.
- OCR fallback: Windows.Media.Ocr (en-GB, built in, winrt wheels) on the foreground window, clipped to its
  monitor, with the focused edit box blanked out so his own typing never counts as 'on screen'. Only run
  when M2 saw the screen change.
Off by default; does nothing unless screen_watch_enabled and screen_text_enabled are both true.
"""
import argparse
import hashlib
import json
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from context_watch import ContextTracker, SNIP

BASE = Path(__file__).resolve().parent
SHADOW_LOG = Path("logs") / "context_shadow.jsonl"
SUPPORTED_MODES = ("log_only",)

DEFAULTS = {
    "screen_text_enabled": False,          # master switch; also needs screen_watch_enabled
    "screen_text_mode": "log_only",        # the only mode built; anything else refuses to start
    "screen_text_min_interval_s": 2.0,     # one pass at most this often
    "screen_text_cpu_max_pct": 60,         # skip a pass when the whole machine is busier than this
    "screen_text_ocr_enabled": True,       # OCR fallback when UI Automation gives no screen text
    "screen_text_ocr_max_pixels": 2_100_000,  # bigger windows are downscaled before OCR (about 1080p)
    "screen_text_ocr_min_interval_s": 30.0,  # OCR no more often than this (09 budget: one burst per 30 s)
    "screen_text_max_chars": 20000,        # text kept per pass (RAM only)
    "screen_text_context_ttl_s": 900,      # entities older than this are forgotten
    "screen_text_hint_threshold": 0.7,     # lowest confidence that gets logged
    "screen_text_other_windows": True,     # also read the other visible windows, not just the focused one
    "screen_text_other_max": 5,            # at most this many other windows (top of the z-order first)
    "screen_text_other_uia_interval_s": 10.0,  # re-read one window's UIA text no more often than this
    "screen_text_other_min_chars": 40,     # UIA text shorter than this falls back to OCR of that window
}


def cfg_get(cfg, key):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


def text_allowed(cfg):
    """(ok, reason). Needs both switches and the log_only mode."""
    if not (cfg or {}).get("screen_watch_enabled", False):
        return False, "screen watch disabled"
    if not cfg_get(cfg, "screen_text_enabled"):
        return False, "disabled"
    mode = cfg_get(cfg, "screen_text_mode")
    if mode not in SUPPORTED_MODES:
        return False, f"unsupported mode {mode!r} (only log_only is built)"
    return True, "ok"


# ------------------------------------------------------------------ shadow log
def append_shadow(path, hint, ts=None):
    """One JSON line per would-be hint. The only thing this module ever writes."""
    d = hint.as_dict()
    rec = {"ts": (ts or datetime.now()).isoformat(timespec="seconds"), "title": d["title"],
           "typed": d["typed"][:SNIP], "entity": d["entity"][:SNIP], "kind": d["kind"],
           "confidence": round(float(d["confidence"]), 3)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def summarise(path=None, base=BASE):
    """Counts from the shadow log so the user can judge a week of data: per day, per kind, per app."""
    path = Path(path) if path else Path(base) / SHADOW_LOG
    per_day, per_kind, per_app, total, bad = Counter(), Counter(), Counter(), 0, 0
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            total += 1
            per_day[str(r.get("ts", ""))[:10]] += 1
            per_kind[r.get("kind", "?")] += 1
            title = str(r.get("title", ""))
            per_app[title.rsplit(" - ", 1)[-1].strip() or "?"] += 1
    return {"total": total, "unreadable": bad, "per_day": dict(sorted(per_day.items())),
            "per_kind": dict(per_kind.most_common()), "per_app": dict(per_app.most_common(10))}


# ------------------------------------------------------------------ real sources (Windows)
class UiaFocus:
    """Focused control via the uiautomation package. Create and call on the worker thread."""

    def __init__(self, max_chars=20000):
        import uiautomation as auto
        self.auto, self.max_chars = auto, max_chars
        self._init = auto.UIAutomationInitializerInThread(debug=False)

    def __call__(self):
        auto = self.auto
        c = auto.GetFocusedControl()
        if c is None:
            return None
        top = c.GetTopLevelControl() or c
        r = top.BoundingRectangle
        out = {"title": (top.Name or "")[:80], "rect": (r.left, r.top, r.right, r.bottom),
               "kind": None, "text": "", "edit_rect": None, "hwnd": getattr(top, "NativeWindowHandle", 0) or 0,
               "titlebar_rect": titlebar_rect((r.left, r.top, r.right, r.bottom),
                                              getattr(top, "NativeWindowHandle", 0) or 0)}
        ctype = c.ControlTypeName
        try:
            if c.GetPropertyValue(auto.PropertyId.IsPasswordProperty):
                out["kind"] = "password"
                return out
        except Exception:
            pass
        er = c.BoundingRectangle
        if ctype == "EditControl":
            text = ""
            try:
                text = c.GetValuePattern().Value or ""
            except Exception:
                try:
                    text = c.GetTextPattern().DocumentRange.GetText(self.max_chars)
                except Exception:
                    text = ""
            out.update(kind="edit", text=text[-self.max_chars:], edit_rect=(er.left, er.top, er.right, er.bottom))
        elif ctype == "DocumentControl":
            try:
                ro = c.GetValuePattern().IsReadOnly
            except Exception:
                ro = True
            try:
                text = c.GetTextPattern().DocumentRange.GetText(self.max_chars)
            except Exception:
                text = ""
            out.update(kind="doc" if ro else "edit", text=text,
                       edit_rect=None if ro else (er.left, er.top, er.right, er.bottom))
        return out


class WinOcr:
    """Windows.Media.Ocr on a BGRA numpy array held in RAM."""

    def __init__(self, lang="en-GB"):
        import asyncio
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import Buffer
        self._asyncio, self._SB, self._fmt, self._Buffer = asyncio, SoftwareBitmap, BitmapPixelFormat, Buffer
        self.engine = OcrEngine.try_create_from_language(Language(lang)) or OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise RuntimeError("no Windows OCR language installed")

    def __call__(self, bgra):
        h, w = bgra.shape[:2]
        data = np.ascontiguousarray(bgra).tobytes()
        buf = self._Buffer(len(data))
        buf.length = len(data)
        memoryview(buf).cast("B")[:] = data
        bmp = self._SB.create_copy_from_buffer(buf, self._fmt.BGRA8, w, h)

        async def run():
            return (await self.engine.recognize_async(bmp)).text
        return self._asyncio.run(run())


class MssRectGrab:
    """Grab a screen rectangle into RAM, clipped to the monitor holding its centre (per-monitor)."""

    def __init__(self):
        import mss
        self._sct = getattr(mss, "MSS", mss.mss)()

    def __call__(self, rect):
        l, t, r, b = rect
        cx, cy = (l + r) // 2, (t + b) // 2
        for m in self._sct.monitors[1:]:
            if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                l, t = max(l, m["left"]), max(t, m["top"])
                r, b = min(r, m["left"] + m["width"]), min(b, m["top"] + m["height"])
                break
        if r - l < 16 or b - t < 16:
            return None, (l, t)
        shot = self._sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        return np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4), (l, t)

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


def mask_rect(bgra, origin, rect):
    """Blank out `rect` (screen coords) in a copy of the frame so the user's typing is not read as context."""
    if rect is None:
        return bgra
    ox, oy = origin
    l, t, r, b = rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy
    h, w = bgra.shape[:2]
    l, t, r, b = max(0, l), max(0, t), min(w, r), min(h, b)
    if r <= l or b <= t:
        return bgra
    out = np.array(bgra, copy=True)
    out[t:b, l:r, :3] = 255
    return out


TITLEBAR_PX_96DPI = 48   # Win11 caption incl. tab strip (Notepad/Terminal) at 100% scaling


def titlebar_rect(rect, hwnd=0):
    """Top band of a window holding its caption. Windows echoes the typed text into the title of many
    editors ('*Please check ticket ... - Notepad'), so this band is never OCR'd as context."""
    if not rect:
        return None
    scale = 1.0
    if hwnd:
        try:
            import ctypes
            scale = max(1.0, ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0)
        except Exception:
            pass
    l, t, r, b = rect
    return (l, t, r, min(b, t + int(round(TITLEBAR_PX_96DPI * scale))))


def overlaps(a, b):
    return bool(a and b) and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


SENSITIVE_TITLE_WORDS = ("password", "passwort", "1password", "bitwarden", "keepass", "lastpass",
                         "credential", "windows security", "sign in", "log in", "login")


def title_sensitive(title):
    low = (title or "").lower()
    return any(w in low for w in SENSITIVE_TITLE_WORDS)


def list_windows(max_windows=5, min_w=200, min_h=120):
    """Visible, non-minimised, non-cloaked top-level windows in z-order (top first), not this process's.
    Cheap (EnumWindows only, no content). Each: {hwnd, title, rect}."""
    import ctypes
    from ctypes import wintypes
    u, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    me = __import__("os").getpid()
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = -20, 0x80, 14

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
            return True
        if u.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wintypes.DWORD(0)
        dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        pid = wintypes.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if title in ("Program Manager",):
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left < min_w or r.bottom - r.top < min_h:
            return True
        out.append({"hwnd": int(hwnd), "title": title[:80], "rect": (r.left, r.top, r.right, r.bottom)})
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return out[:max(0, int(max_windows)) * 3]     # caller filters further and applies the real cap


class UiaWindowText:
    """Text of one top-level window via UI Automation: document/edit TextPattern or ValuePattern, plus Names
    of text controls. Bounded by depth, node count and time. Password boxes are skipped. Worker thread only."""

    def __init__(self, max_chars=20000, max_depth=8, max_nodes=400, max_s=0.25):
        import uiautomation as auto
        self.auto, self.max_chars, self.max_depth, self.max_nodes, self.max_s = auto, max_chars, max_depth, max_nodes, max_s
        self._init = auto.UIAutomationInitializerInThread(debug=False)

    def __call__(self, hwnd):
        auto = self.auto
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return ""
        parts, total, nodes, t0 = [], 0, 0, time.perf_counter()
        for c, _depth in auto.WalkControl(root, includeTop=False, maxDepth=self.max_depth):
            nodes += 1
            if nodes > self.max_nodes or time.perf_counter() - t0 > self.max_s or total >= self.max_chars:
                break
            ct = c.ControlTypeName
            txt = ""
            if ct in ("DocumentControl", "EditControl"):
                try:
                    if c.GetPropertyValue(auto.PropertyId.IsPasswordProperty):
                        continue
                except Exception:
                    pass
                try:
                    txt = c.GetTextPattern().DocumentRange.GetText(self.max_chars)
                except Exception:
                    try:
                        txt = c.GetValuePattern().Value or ""
                    except Exception:
                        txt = ""
            elif ct in ("TextControl", "ListItemControl", "DataItemControl", "HyperlinkControl"):
                txt = c.Name or ""
            if txt:
                parts.append(txt)
                total += len(txt)
        return "\n".join(parts)[:self.max_chars]


def limit_pixels(bgra, max_pixels):
    """Integer-stride downscale until under the pixel cap (cheap, no copy beyond the view)."""
    h, w = bgra.shape[:2]
    f = 1
    while (h // f) * (w // f) > max_pixels:
        f += 1
    return bgra if f == 1 else np.ascontiguousarray(bgra[::f, ::f])


def _cpu_percent():
    import psutil
    return psutil.cpu_percent(interval=None)


FAIL_QUIET_AFTER = 3     # log the first N consecutive failures, then stay quiet until a pass succeeds
FAIL_BACKOFF_MAX_S = 300


def fail_backoff(fails):
    """Seconds to wait after the Nth consecutive failure: 5, 10, 20 ... capped at 5 minutes."""
    return min(FAIL_BACKOFF_MAX_S, 5 * 2 ** max(0, fails - 1))


def describe_error(e):
    """'Type: message at file:line' from the innermost frame. Message is cut short; no screen content is in it."""
    import os
    import traceback
    where = ""
    tb = traceback.extract_tb(e.__traceback__) if e.__traceback__ else []
    if tb:
        f = tb[-1]
        where = f" at {os.path.basename(f.filename)}:{f.lineno}"
    msg = str(e).replace("\n", " ")[:160]
    return f"{type(e).__name__}: {msg}{where}" if msg else f"{type(e).__name__}{where}"


def _digest(*parts):
    return hashlib.blake2b("\x00".join(parts).encode("utf-8", "ignore"), digest_size=16).digest()


# ------------------------------------------------------------------ the text tier
class ScreenText:
    """start()/stop() plus tick() (public so tests can drive it). `watch` is the M2 ScreenWatch."""

    def __init__(self, cfg=None, log=None, watch=None, focus_fn=None, ocr_fn=None, grab_fn=None,
                 cpu_fn=None, clock=time.monotonic, base=BASE, tracker=None, sleep=None,
                 windows_fn=None, window_text_fn=None):
        self.cfg, self.base = dict(cfg or {}), Path(base)
        self.log = log or (lambda who, text: None)
        self.watch, self.clock = watch, clock
        self._focus_fn, self._ocr_fn, self._grab_fn = focus_fn, ocr_fn, grab_fn
        # other windows: windows_fn() -> [{hwnd, title, rect}] in z-order; window_text_fn(hwnd) -> UIA text.
        # When the focus source is injected (tests) these stay off unless injected too.
        self._windows_fn, self._window_text_fn = windows_fn, window_text_fn
        self._win_cache = {}      # hwnd -> {"uia_ts", "ocr_ts", "title", "rect"}
        self._factories = focus_fn is None or ocr_fn is None or grab_fn is None
        self.cpu_fn = cpu_fn or _cpu_percent
        self.tracker = tracker or ContextTracker(float(cfg_get(self.cfg, "screen_text_context_ttl_s")),
                                                 threshold=float(cfg_get(self.cfg, "screen_text_hint_threshold")),
                                                 clock=clock)
        self.shadow_path = self.base / SHADOW_LOG
        self.counters = Counter()
        self._last_ctx, self._last_typed = {}, {}
        self._last_ocr_ts = None
        self._last_change_ts = None
        self.last_pass_ts = None
        self._stop = threading.Event()
        self._thread = None
        self._sleep = sleep or (lambda s: self._stop.wait(s))

    # ---- control
    def start(self):
        ok, why = text_allowed(self.cfg)
        if not ok:
            self.log("watch", f"screen text not started: {why}")
            return False
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="screen_text", daemon=True)
        self._thread.start()
        self.log("watch", "screen text started (log only, RAM only)")
        return True

    def stop(self):
        was = self.running
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        self.tracker.clear()
        self._last_ctx.clear()
        self._last_typed.clear()
        self._win_cache.clear()
        if was:
            self.log("watch", f"screen text stopped ({self.counters['passes']} passes, {self.counters['hints']} hints logged)")

    kill = stop

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    # ---- one pass
    def _screen_changed(self):
        if self.watch is None:
            return True
        ch = self.watch.recent_changes(self._last_change_ts)
        ch = [c for c in ch if self._last_change_ts is None or c["ts"] > self._last_change_ts]
        if ch:
            self._last_change_ts = max(c["ts"] for c in ch)
            return True
        return False

    def tick(self):
        """One pass. Returns a short outcome string (for tests and counters)."""
        ok, why = text_allowed(self.cfg)
        if not ok:
            return "not allowed"
        self.counters["passes"] += 1
        self.last_pass_ts = self.clock()             # watch_status.py reports its age
        if self.watch is not None and not self.watch.watching:
            self.counters["skipped_paused"] += 1
            return "watch paused"
        if float(self.cpu_fn()) > float(cfg_get(self.cfg, "screen_text_cpu_max_pct")):
            self.counters["skipped_cpu"] += 1
            return "cpu busy"
        focus = self._focus_fn() if self._focus_fn else None
        if not focus:
            return "no focus"
        title = focus.get("title", "")
        kind = focus.get("kind")
        if kind == "password":
            self.counters["skipped_password"] += 1
            return "password field"
        maxc = int(cfg_get(self.cfg, "screen_text_max_chars"))

        memo = []

        def changed():                       # M2 change events are consumed once per pass
            if not memo:
                memo.append(self._screen_changed())
            return memo[0]

        # context: UIA document text first, OCR of the foreground window as fallback
        ctx, source = "", ""
        if kind == "doc" and focus.get("text"):
            ctx, source = focus["text"][:maxc], "uia"
        elif cfg_get(self.cfg, "screen_text_ocr_enabled") and self._ocr_fn and self._grab_fn and focus.get("rect"):
            now = self.clock()
            gap = float(cfg_get(self.cfg, "screen_text_ocr_min_interval_s"))
            if (self._last_ocr_ts is None or now - self._last_ocr_ts >= gap) and changed():
                frame, origin = self._grab_fn(focus["rect"])
                if frame is not None:
                    frame = mask_rect(frame, origin, focus.get("edit_rect"))
                    # his typing echoes into the caption ('*Please check ticket ... - Notepad'): never read it
                    tb = focus.get("titlebar_rect") or titlebar_rect(focus["rect"])
                    frame = mask_rect(frame, origin, tb)
                    frame = limit_pixels(frame, int(cfg_get(self.cfg, "screen_text_ocr_max_pixels")))
                    ctx, source = (self._ocr_fn(frame) or "")[:maxc], "ocr"
                    self._last_ocr_ts = now
                    self.counters["ocr"] += 1
                del frame
        outcome = "nothing new"
        if ctx:
            d = _digest(title, ctx)
            if self._last_ctx.get(title) == d:
                self.counters["skipped_unchanged"] += 1
                outcome = "context unchanged"
            else:
                self._last_ctx[title] = d
                self.tracker.observe(title, ctx, source)
                self.counters[f"context_{source}"] += 1
                outcome = "context updated"
        del ctx

        # the other visible windows (the reference he is copying from is usually NOT the focused one)
        if cfg_get(self.cfg, "screen_text_other_windows") and self._windows_fn:
            if self._read_other_windows(focus, maxc, changed):
                outcome = "context updated"

        # what he is typing (re-checked when new context arrived, so late-read references still count)
        typed = focus.get("text", "") if kind == "edit" else ""
        if typed:
            d = _digest(title, typed)
            if self._last_typed.get(title) == d and outcome != "context updated":
                return outcome
            self._last_typed[title] = d
            for h in self.tracker.check_typed(title, typed[-maxc:]):
                append_shadow(self.shadow_path, h)
                self.counters["hints"] += 1
                outcome = "hint logged"
        return outcome

    def _read_other_windows(self, focus, maxc, changed):
        """Feed text from up to N other visible windows into the tracker. UIA first (cheap, re-read at most
        every screen_text_other_uia_interval_s per window); OCR of the window's rect only when UIA gives too
        little, at most one window per pass, per window no more often than the OCR interval, and only after
        M2 saw a change. Pixels of the focused window and of anything above the target are blanked first, so
        his own typing can never come back in through an overlapping grab. Returns True if context changed."""
        t0 = time.perf_counter()
        wins = list(self._windows_fn() or [])
        cap = int(cfg_get(self.cfg, "screen_text_other_max"))
        uia_gap = float(cfg_get(self.cfg, "screen_text_other_uia_interval_s"))
        ocr_gap = float(cfg_get(self.cfg, "screen_text_ocr_min_interval_s"))
        min_chars = int(cfg_get(self.cfg, "screen_text_other_min_chars"))
        ocr_ok = cfg_get(self.cfg, "screen_text_ocr_enabled") and self._ocr_fn and self._grab_fn
        fh, frect = focus.get("hwnd"), focus.get("rect")
        now = self.clock()
        covering = [frect] if frect else []
        picked, updated, ocr_used = [], False, False
        for w in wins:
            if len(picked) >= cap:
                break
            h = w.get("hwnd")
            if (fh and h == fh) or (not fh and w.get("rect") == frect and w.get("title") == focus.get("title")):
                continue
            if title_sensitive(w.get("title")):
                self.counters["skipped_sensitive_window"] += 1
                covering.append(w.get("rect"))
                continue
            picked.append((w, list(covering)))
            covering.append(w.get("rect"))
        alive = {w.get("hwnd") for w, _ in picked}
        for h in [h for h in self._win_cache if h not in alive]:
            del self._win_cache[h]
            self._last_ctx.pop(f"win:{h}", None)
        for w, above in picked:
            h, wtitle = w.get("hwnd"), w.get("title", "")
            c = self._win_cache.setdefault(h, {"uia_ts": None, "ocr_ts": None, "uia_len": 0, "title": None})
            text, src = "", ""
            if self._window_text_fn and (c["uia_ts"] is None or now - c["uia_ts"] >= uia_gap or c["title"] != wtitle):
                c["uia_ts"], c["title"] = now, wtitle
                try:
                    text = (self._window_text_fn(h) or "")[:maxc]
                except Exception:
                    text = ""
                c["uia_len"] = len(text.strip())
                self.counters["window_uia"] += 1
                src = "uia"
                if len(text.strip()) < min_chars:
                    text = ""
            if (not text and ocr_ok and not ocr_used and c["uia_len"] < min_chars and w.get("rect")
                    and (c["ocr_ts"] is None or now - c["ocr_ts"] >= ocr_gap) and changed()):
                frame, origin = self._grab_fn(w["rect"])
                if frame is not None:
                    for r in above:
                        if overlaps(r, w["rect"]):
                            frame = mask_rect(frame, origin, r)
                    frame = limit_pixels(frame, int(cfg_get(self.cfg, "screen_text_ocr_max_pixels")))
                    text, src = (self._ocr_fn(frame) or "")[:maxc], "ocr"
                    self.counters["window_ocr"] += 1
                c["ocr_ts"], ocr_used = now, True
                del frame
            if text:
                key = f"win:{h}"
                d = _digest(wtitle, text)
                if self._last_ctx.get(key) != d:
                    self._last_ctx[key] = d
                    self.tracker.observe(wtitle, text, src)
                    self.counters[f"context_window_{src}"] += 1
                    updated = True
            del text
        self.counters["window_ms_total"] += int((time.perf_counter() - t0) * 1000)
        return updated

    def _make_sources(self):
        if self._focus_fn is None:
            self._focus_fn = UiaFocus(int(cfg_get(self.cfg, "screen_text_max_chars")))
        if cfg_get(self.cfg, "screen_text_other_windows"):
            try:
                if self._windows_fn is None:
                    cap = int(cfg_get(self.cfg, "screen_text_other_max"))
                    self._windows_fn = lambda: list_windows(cap)
                if self._window_text_fn is None:
                    self._window_text_fn = UiaWindowText(int(cfg_get(self.cfg, "screen_text_max_chars")))
            except Exception as e:
                self.log("watch", f"screen text other-window reading unavailable: {type(e).__name__}")
                self._windows_fn = None
        if cfg_get(self.cfg, "screen_text_ocr_enabled"):
            try:
                if self._ocr_fn is None:
                    self._ocr_fn = WinOcr()
                if self._grab_fn is None:
                    self._grab_fn = MssRectGrab()
            except Exception as e:
                self.log("watch", f"screen text OCR unavailable: {type(e).__name__}")
                self._ocr_fn = None

    def _loop(self):
        try:
            self._make_sources()
        except Exception as e:
            self.log("watch", f"screen text failed to start: {type(e).__name__}")
            return
        fails = 0
        while not self._stop.is_set():
            try:
                self.tick()
                if fails >= FAIL_QUIET_AFTER:
                    self.log("watch", f"screen text pass recovered after {fails} failures")
                fails = 0
            except Exception as e:
                self.counters["errors"] += 1
                fails += 1
                if fails <= FAIL_QUIET_AFTER:
                    self.log("watch", f"screen text pass failed: {describe_error(e)}"
                             + (" (going quiet, backing off)" if fails == FAIL_QUIET_AFTER else ""))
                self._sleep(fail_backoff(fails))
            self._sleep(float(cfg_get(self.cfg, "screen_text_min_interval_s")))
        g = self._grab_fn
        if hasattr(g, "close"):
            g.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Summarise the context shadow log.")
    ap.add_argument("--summary", action="store_true")
    ap.parse_args()
    print(json.dumps(summarise(), indent=2))
