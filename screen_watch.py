"""Live assistant M2: screen capture into a RAM-only ring buffer with change detection.

Quiet background piece. It never speaks, never calls a model or the network, and writes nothing to
disk: kept frames are JPEG bytes held in RAM and dropped on pause, stop and exit. Only counters go to
the log (no pixels, no window titles, no text).

Design follows research/live_assistant/02_capture.md (mss grab, green-channel stride sample, settle
rule, JPEG ring in RAM), 09_performance_budget.md (<= 1% of one core, stop on games/fullscreen) and
07_privacy_and_safety.md (off by default, ring capped and zeroed on pause). The user dropped the
work-laptop gate (10 Oct 2026): RAM only, so no new exposure.

Layout: pure, testable core (sample/diff, ChangeDetector, FrameRing, RateController, gates) and a
thin capture backend (MssBackend, foreground/fullscreen guard) used by the ScreenWatch thread.
"""
import io
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent

DEFAULTS = {
    "screen_watch_enabled": False,          # master switch; off unless the user turns it on
    "screen_watch_active_interval_s": 1.0,  # sample rate while the screen is changing / just changed
    "screen_watch_idle_interval_s": 5.0,    # sample rate when nothing has changed for a while
    "screen_watch_active_hold_s": 10.0,     # stay at the fast rate this long after the last change
    "screen_watch_ring_seconds": 60,        # kept frames older than this are dropped
    "screen_watch_ring_frames": 10,         # per monitor
    "screen_watch_ring_mb": 4,              # whole ring, all monitors
    "screen_watch_threshold": 0.005,        # fraction of sample cells that must move to count as a change
    "screen_watch_level": 10,               # grey-level difference that counts as a moved cell
    "screen_watch_settle": 2,               # unchanged samples needed after a change before it counts (debounce)
    "screen_watch_moving_keep_s": 4.0,      # screen changed but never settles (games, video): keep a frame anyway
                                            # after this long, ring only (not a 'change' event); 0 = never
    "screen_watch_monitors": "all",         # "all" or a list of mss monitor numbers (1-based)
    "screen_watch_pause_on_fullscreen": False,   # 11 Oct 2026: capture everything, games included
    "screen_watch_game_names": ["wow.exe", "wowclassic.exe", "wowb.exe", "wowt.exe"],
    "screen_watch_max_width": 960,          # kept JPEG width cap
    "screen_watch_jpeg_quality": 60,
}


def cfg_get(cfg, key):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


# ------------------------------------------------------------------ pure core
def sample(bgra, stride=8):
    """Cheap change sample: green channel every `stride` pixels (zero copy view, about 0.02 ms)."""
    return bgra[::stride, ::stride, 1]


def change_fraction(a, b, level=10):
    """Fraction of sample cells whose value moved by more than `level`. Shape mismatch counts as 1.0."""
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return float(np.count_nonzero(d > level)) / float(d.size or 1)


class ChangeDetector:
    """Per-monitor change gate with a settle (debounce) rule.

    A change against the last kept baseline only becomes a 'significant change' event once the screen
    has stopped moving for `settle` consecutive samples, so scroll and animation mid-frames are skipped.
    The first sample always produces an event (fraction 1.0) so the ring has a starting frame.
    """

    def __init__(self, threshold=0.005, level=10, settle=2, moving_keep_s=0.0):
        self.threshold, self.level, self.settle = threshold, level, max(0, int(settle))
        self.moving_keep_s = float(moving_keep_s or 0)
        self.baseline = None
        self.prev = None
        self.still = 0
        self.unsettled_since = None                 # ts the screen first differed from baseline without settling

    def reset(self):
        self.baseline = self.prev = None
        self.still = 0
        self.unsettled_since = None

    def feed(self, smp, ts):
        """Return an event dict {'ts', 'fraction'} or None. `smp` is a sample() array."""
        smp = np.array(smp, copy=True)              # own copy: the grab buffer is reused/freed
        if self.baseline is None:
            self.baseline = self.prev = smp
            return {"ts": ts, "fraction": 1.0}
        motion = change_fraction(self.prev, smp, self.level)
        self.prev = smp
        vs_base = change_fraction(self.baseline, smp, self.level)
        if vs_base <= self.threshold:
            self.still = 0
            self.unsettled_since = None
            return None
        self.still = self.still + 1 if motion <= self.threshold else 0
        if self.still >= self.settle:
            self.baseline, self.still, self.unsettled_since = smp, 0, None
            return {"ts": ts, "fraction": round(vs_base, 4)}
        # A game or video never stops moving, so the settle rule above never fires and no frame would ever be kept
        # (the 11 Oct 2026 silent-commentator bug). After moving_keep_s of unsettled change, keep one anyway.
        if self.unsettled_since is None:
            self.unsettled_since = ts
        elif self.moving_keep_s > 0 and ts - self.unsettled_since >= self.moving_keep_s:
            self.baseline, self.still, self.unsettled_since = smp, 0, None
            return {"ts": ts, "fraction": round(vs_base, 4), "moving": True}
        return None


class FrameRing:
    """RAM-only ring of kept frames (encoded bytes), capped by count per monitor, total bytes and age."""

    def __init__(self, max_frames=10, max_bytes=4 * 1024 * 1024, max_age_s=60, clock=time.monotonic):
        self.max_frames, self.max_bytes, self.max_age_s, self.clock = max_frames, max_bytes, max_age_s, clock
        self._items = deque()                      # (monitor, ts, bytearray, meta)
        self._lock = threading.Lock()

    def push(self, monitor, data, meta=None):
        buf = bytearray(data)
        with self._lock:
            self._items.append((monitor, self.clock(), buf, dict(meta or {})))
            self._evict()

    def _evict(self):
        now = self.clock()
        while self._items and now - self._items[0][1] > self.max_age_s:
            self._drop(0)
        for mon in {m for m, *_ in self._items}:
            idx = [i for i, it in enumerate(self._items) if it[0] == mon]
            for i in reversed(idx[:max(0, len(idx) - self.max_frames)]):
                self._drop(i)
        while self._items and self.nbytes() > self.max_bytes:
            self._drop(0)

    def _drop(self, i):
        item = self._items[i]
        del self._items[i]
        item[2][:] = b"\0" * len(item[2])          # best-effort zeroing before release

    def nbytes(self):
        return sum(len(it[2]) for it in self._items)

    def __len__(self):
        return len(self._items)

    def latest(self, monitor):
        with self._lock:
            self._evict()
            for m, ts, buf, meta in reversed(self._items):
                if m == monitor:
                    return {"monitor": m, "ts": ts, "jpeg": bytes(buf), **meta}
        return None

    def stats(self):
        """{monitor: {"frames", "bytes", "newest_ts"}}: sizes and times only, no image data."""
        out = {}
        with self._lock:
            for m, ts, buf, _meta in self._items:
                s = out.setdefault(m, {"frames": 0, "bytes": 0, "newest_ts": ts})
                s["frames"] += 1
                s["bytes"] += len(buf)
                s["newest_ts"] = max(s["newest_ts"], ts)
        return out

    def clear(self):
        with self._lock:
            while self._items:
                self._drop(0)

    def drop_monitor(self, monitor):
        """Wipe every kept frame for one monitor (it vanished or its rect changed)."""
        with self._lock:
            for i in reversed([i for i, it in enumerate(self._items) if it[0] == monitor]):
                self._drop(i)


class RateController:
    """Adaptive rate: fast while things change, slow when idle."""

    def __init__(self, active_s=1.0, idle_s=5.0, hold_s=10.0, clock=time.monotonic):
        self.active_s, self.idle_s, self.hold_s, self.clock = active_s, idle_s, hold_s, clock
        self.last_change = None

    def note_change(self):
        self.last_change = self.clock()

    def interval(self):
        if self.last_change is not None and self.clock() - self.last_change <= self.hold_s:
            return self.active_s
        return self.idle_s


def watch_allowed(cfg):
    """(ok, reason). Off by default. No per-machine gate (10 Oct 2026: frames never leave RAM).
    Keeping any frame or recording on disk is a separate decision for a later milestone, not built here."""
    if not cfg_get(cfg, "screen_watch_enabled"):
        return False, "disabled"
    return True, "ok"


def encode_jpeg(bgra, max_width=960, quality=60):
    """BGRA numpy frame -> JPEG bytes in RAM (integer box reduce, never touches disk)."""
    from PIL import Image
    h, w = bgra.shape[:2]
    img = Image.frombuffer("RGBA", (w, h), np.ascontiguousarray(bgra).tobytes(), "raw", "BGRA", 0, 1).convert("RGB")
    f = 1
    while w // f > max_width:
        f += 1
    if f > 1:
        img = img.reduce(f)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


# ------------------------------------------------------------------ thin backend
class MssBackend:
    """mss GDI grab. Must be created and used on the capture thread (mss handles are thread-bound)."""

    def __init__(self):
        import mss
        self._mss = mss
        self._sct = getattr(mss, "MSS", mss.mss)()

    def monitors(self):
        return list(range(1, len(self._sct.monitors)))   # index 0 is the virtual all-in-one screen

    def layout(self):
        """{monitor number: (left, top, width, height)} from mss's list (cached by mss until refresh())."""
        return {i: (m["left"], m["top"], m["width"], m["height"]) for i, m in enumerate(self._sct.monitors) if i}

    def signature(self):
        """Cheap hot-plug signal (no enumeration): monitor count + virtual-screen rect. None if unavailable."""
        try:
            import ctypes
            g = ctypes.windll.user32.GetSystemMetrics
            return (g(80), g(76), g(77), g(78), g(79))   # SM_CMONITORS, SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN
        except Exception:
            return None

    def refresh(self):
        """mss enumerates monitors once per object and never again, so recreate it to re-enumerate."""
        self.close()
        self._sct = getattr(self._mss, "MSS", self._mss.mss)()

    def grab(self, monitor):
        shot = self._sct.grab(self._sct.monitors[monitor])
        return np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


class PilGrabBackend:
    """Second GDI path via Pillow's ImageGrab (all_screens). Uses mss-style monitor numbering from a layout."""

    def __init__(self, layout_fn):
        from PIL import ImageGrab
        self._ig, self._layout_fn = ImageGrab, layout_fn

    def grab(self, monitor):
        rect = self._layout_fn().get(monitor)
        if rect is None:
            raise LookupError(f"no rect for monitor {monitor}")
        l, t, w, h = rect
        img = self._ig.grab(bbox=(l, t, l + w, t + h), all_screens=True).convert("RGBA")
        a = np.asarray(img)
        return np.ascontiguousarray(a[..., [2, 1, 0, 3]])       # RGBA -> BGRA like mss

    def close(self):
        pass


class DxcamBackend:
    """DXGI Desktop Duplication via dxcam (optional; sees D3D fullscreen apps). Lazy import; matches
    each mss monitor to a dxcam output by size so numbering stays mss-style."""

    def __init__(self, layout_fn):
        import dxcam
        self._dx, self._layout_fn, self._cams, self._last = dxcam, layout_fn, {}, {}

    def _cam(self, monitor):
        cam = self._cams.get(monitor)
        if cam is None:
            rect = self._layout_fn().get(monitor)
            if rect is None:
                raise LookupError(f"no rect for monitor {monitor}")
            cam = self._dx.create(output_idx=monitor - 1, output_color="BGRA")
            if cam is None or (cam.width, cam.height) != (rect[2], rect[3]):
                raise LookupError(f"dxcam output {monitor - 1} does not match monitor {monitor}")
            self._cams[monitor] = cam
        return cam

    def grab(self, monitor):
        f = self._cam(monitor).grab()
        if f is None:                          # dxcam returns None when nothing changed since last grab
            f = self._last.get(monitor)
            if f is None:
                raise RuntimeError("dxcam: no frame yet")
        self._last[monitor] = f
        return f

    def close(self):
        for c in self._cams.values():
            try:
                c.release()
            except Exception:
                pass
        self._cams.clear()
        self._last.clear()


def _blank(frame):
    """True for an all-black frame (a failed capture of a protected/fullscreen surface)."""
    try:
        s = frame[::16, ::16, :3]
        return int(s.max()) <= 2
    except Exception:
        return True


class FallbackBackend:
    """Primary mss plus a chain of alternates behind the same interface (monitors/layout/signature/
    refresh/grab/close). On a failed or all-black grab the next backend is tried; the one that worked
    is remembered per monitor and the primary is retried every PRIMARY_RETRY_S. Optional backends
    (dxcam) import lazily and are skipped quietly if missing. Frames stay in RAM."""

    PRIMARY_RETRY_S = 60.0

    def __init__(self, primary_factory=None, alternates=None, clock=time.monotonic):
        self.clock = clock
        self._primary = (primary_factory or MssBackend)()
        if alternates is None:
            alternates = [("dxcam", DxcamBackend), ("pil", PilGrabBackend)]
        self._alt_specs = list(alternates)        # [(name, factory(layout_fn))]
        self._alts = {}                           # name -> instance, or None if unavailable
        self.using = {}                           # monitor -> backend name currently working
        self._retry_at = {}                       # monitor -> when to try the primary again

    # interface passthrough
    def monitors(self):
        return self._primary.monitors()

    def layout(self):
        if hasattr(self._primary, "layout"):
            return self._primary.layout()
        return {m: None for m in self._primary.monitors()}

    def signature(self):
        return self._primary.signature() if hasattr(self._primary, "signature") else None

    def refresh(self):
        if hasattr(self._primary, "refresh"):
            self._primary.refresh()
        self._close_alts()                         # outputs may have moved; rebuild lazily

    def _alt(self, name, factory):
        if name not in self._alts:
            try:
                self._alts[name] = factory(self.layout)
            except Exception:
                self._alts[name] = None            # not installed / not usable: skip quietly
        return self._alts[name]

    def _chain(self, monitor):
        cur = self.using.get(monitor, "primary")
        order = [("primary", None)] + self._alt_specs
        if cur != "primary" and self.clock() < self._retry_at.get(monitor, 0):
            order = [o for o in order if o[0] == cur] + [o for o in order if o[0] not in (cur, "primary")] \
                + [("primary", None)]
        return order

    def grab(self, monitor):
        first_err, blank = None, None
        for name, factory in self._chain(monitor):
            be = self._primary if name == "primary" else self._alt(name, factory)
            if be is None:
                continue
            try:
                f = be.grab(monitor)
            except Exception as e:
                if first_err is None:
                    first_err = e
                if name != "primary":                # drop a broken alternate so it is rebuilt next time
                    try:
                        be.close()
                    except Exception:
                        pass
                    self._alts.pop(name, None)
                continue
            if _blank(f):
                blank = blank if blank is not None else (name, f)
                continue
            self._note(monitor, name)
            return f
        if blank is not None:                        # everything black: maybe the screen really is black
            self._note(monitor, blank[0])
            return blank[1]
        raise first_err or RuntimeError("no capture backend available")

    def _note(self, monitor, name):
        if name == "primary":
            self.using.pop(monitor, None)
            self._retry_at.pop(monitor, None)
        else:
            if self.using.get(monitor) != name or self.clock() >= self._retry_at.get(monitor, 0):
                self._retry_at[monitor] = self.clock() + self.PRIMARY_RETRY_S
            self.using[monitor] = name

    def _close_alts(self):
        for be in self._alts.values():
            if be is not None:
                try:
                    be.close()
                except Exception:
                    pass
        self._alts.clear()

    def close(self):
        self._close_alts()
        try:
            self._primary.close()
        except Exception:
            pass


def foreground_blocks(game_names):
    """True when a game or fullscreen app is in front (09: stop capture entirely). Windows only, cheap."""
    try:
        import ctypes
        from ctypes import wintypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        state = ctypes.c_int(0)
        try:
            if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4):
                return True                       # busy / D3D fullscreen / presentation mode
        except Exception:
            pass
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)    # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf = ctypes.create_unicode_buffer(260)
                n = wintypes.DWORD(260)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    if os.path.basename(buf.value).lower() in {g.lower() for g in game_names}:
                        return True
            finally:
                k.CloseHandle(h)
    except Exception:
        return False
    return False


# ------------------------------------------------------------------ the watcher
class ScreenWatch:
    """start()/stop()/pause()/resume()/kill() plus RAM accessors for later milestones.

    paused_fn: optional callable returning a reason string (or '') when the app says "not watching"
    (e.g. Mute). The PAUSE flag file state/live_watch/PAUSE is also checked each tick (read only).
    """

    def __init__(self, cfg=None, log=None, backend_factory=FallbackBackend, paused_fn=None,
                 guard_fn=None, clock=time.monotonic, sleep=None, base=BASE):
        self.cfg, self.base = dict(cfg or {}), Path(base)
        self.log = log or (lambda who, text: None)
        self.backend_factory, self.paused_fn, self.clock = backend_factory, paused_fn, clock
        names = cfg_get(self.cfg, "screen_watch_game_names")
        if guard_fn is None and cfg_get(self.cfg, "screen_watch_pause_on_fullscreen"):
            guard_fn = lambda: foreground_blocks(names)
        self.guard_fn = guard_fn
        self.ring = FrameRing(int(cfg_get(self.cfg, "screen_watch_ring_frames")),
                              int(float(cfg_get(self.cfg, "screen_watch_ring_mb")) * 1024 * 1024),
                              float(cfg_get(self.cfg, "screen_watch_ring_seconds")), clock)
        self.rate = RateController(float(cfg_get(self.cfg, "screen_watch_active_interval_s")),
                                   float(cfg_get(self.cfg, "screen_watch_idle_interval_s")),
                                   float(cfg_get(self.cfg, "screen_watch_active_hold_s")), clock)
        self.detectors = {}
        self.changes = deque(maxlen=200)          # metadata only: ts, monitor, fraction
        self.counters = {"ticks": 0, "grabs": 0, "events": 0, "skipped": 0, "errors": 0, "monitor_changes": 0}
        self.per_monitor = {}                     # monitor -> {grabs, errors, last_error} (watch_status.py)
        self._layout = None                       # {monitor: rect} last seen; None = not enumerated yet
        self._sig = None                          # last cheap hot-plug signature
        self._recheck = False                     # force a re-enumeration on the next pass
        self._passes_since_enum = 0
        self._backoff_until = 0.0
        self._fail = {}                           # monitor -> failure streak (for rate-limited logging)
        self._using_logged = {}                   # monitor -> backend name last reported
        self._paused_reason = ""                  # manual pause
        self._skip_reason = ""                    # automatic (mute, game, flag file)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._backend = None
        self._sleep = sleep or (lambda s: self._wake.wait(s))

    # ---- control
    def start(self):
        ok, why = watch_allowed(self.cfg)
        if not ok:
            self.log("watch", f"screen watch not started: {why}")
            return False
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="screen_watch", daemon=True)
        self._thread.start()
        self.log("watch", "screen watch started (RAM only)")
        return True

    def stop(self):
        """Idempotent. Ends the thread and wipes the ring."""
        was = self.running
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        self._wipe()
        if was:
            self.log("watch", f"screen watch stopped ({self.counters['grabs']} grabs, {self.counters['events']} changes)")

    def pause(self, reason="paused"):
        self._paused_reason = reason or "paused"
        self._wipe()

    def resume(self):
        self._paused_reason = ""
        self._wake.set()

    kill = stop                                   # quick kill switch for a hotkey / voice command

    def _wipe(self):
        self.ring.clear()
        for d in self.detectors.values():
            d.reset()

    # ---- status / accessors
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def watching(self):
        """True only while frames are actually being taken (for an obvious UI indicator)."""
        return self.running and not self._paused_reason and not self._skip_reason

    def status(self):
        if not self.running:
            state = "off"
        elif self._paused_reason:
            state = f"paused: {self._paused_reason}"
        elif self._skip_reason:
            state = f"paused: {self._skip_reason}"
        else:
            state = "watching"
        return {"state": state, "watching": self.watching, "interval_s": self.rate.interval(),
                "ring_frames": len(self.ring), "ring_bytes": self.ring.nbytes(), **self.counters}

    def get_latest_frame(self, monitor):
        return self.ring.latest(monitor)

    def monitor_rects(self):
        """Current {monitor: (left, top, width, height)} (rect None for backends without layout())."""
        return dict(self._layout or {})

    @property
    def monitor_changes(self):
        """Bumps every time the monitor set or a monitor's rect changes (M3 can drop per-monitor state)."""
        return self.counters["monitor_changes"]

    def recent_changes(self, since=None):
        return [c for c in list(self.changes) if since is None or c["ts"] >= since]

    # ---- loop
    def _auto_pause_reason(self):
        try:
            if self.paused_fn is not None:
                r = self.paused_fn()
                if r:
                    return str(r)
            if (self.base / "state" / "live_watch" / "PAUSE").exists():
                return "flag file"
            if self.guard_fn is not None and self.guard_fn():
                return "game/fullscreen"
        except Exception:
            return "check failed"
        return ""

    RECHECK_EVERY = 30        # full re-enumeration every N passes even if the cheap signal says nothing moved
    GRAB_BACKOFF_S = 2.0      # pause after a failed grab before trying again

    FAIL_LOG_EVERY_S = 60.0   # repeat failure log at most this often (with a count)

    def _note_fail(self, mon, e):
        st = self._fail.get(mon)
        now = self.clock()
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        if st is None:
            self._fail[mon] = {"since": now, "count": 1, "logged": now, "unlogged": 0}
            self.log("watch", f"watch: grab failed on monitor {mon} ({msg}), rechecking monitors")
            return
        st["count"] += 1
        st["unlogged"] += 1
        if now - st["logged"] >= self.FAIL_LOG_EVERY_S:
            self.log("watch", f"watch: grab still failing on monitor {mon} ({msg}); "
                              f"{st['unlogged']} more failures in the last {int(now - st['logged'])} s")
            st["logged"], st["unlogged"] = now, 0

    def _note_ok(self, mon):
        st = self._fail.pop(mon, None)
        using = getattr(self._backend, "using", {}).get(mon, "primary")
        if st is not None:
            self.log("watch", f"watch: monitor {mon} capturing again via {using} after {st['count']} "
                              f"failures over {int(self.clock() - st['since'])} s")
        if using != self._using_logged.get(mon, "primary"):
            self.log("watch", f"watch: monitor {mon} now captured via {using}")
            self._using_logged[mon] = using

    def _read_layout(self):
        be = self._backend
        if hasattr(be, "layout"):
            return dict(be.layout())
        return {m: None for m in be.monitors()}

    def _check_monitors(self):
        """Re-enumerate on a hot-plug signal, after a failed grab, or every RECHECK_EVERY passes.
        Monitors that vanished or whose rect changed lose their detector and kept frames."""
        be = self._backend
        sig = be.signature() if hasattr(be, "signature") else None
        self._passes_since_enum += 1
        if (self._layout is not None and not self._recheck and sig == self._sig
                and self._passes_since_enum < self.RECHECK_EVERY):
            return
        if self._layout is not None and hasattr(be, "refresh"):
            be.refresh()
        new = self._read_layout()
        self._sig, self._recheck, self._passes_since_enum = sig, False, 0
        old = self._layout
        self._layout = new
        if old is None or old == new:
            return
        for mon in set(old) | set(self.detectors):
            if mon not in new or old.get(mon) != new[mon]:
                self.detectors.pop(mon, None)
                self.ring.drop_monitor(mon)
        self.counters["monitor_changes"] += 1
        self.log("watch", f"watch: monitors changed {len(old)} -> {len(new)}")

    def tick(self):
        """One sampling pass over the chosen monitors. Public so tests can drive it without a thread."""
        self.counters["ticks"] += 1
        if self._paused_reason:
            return
        reason = self._auto_pause_reason()
        if reason != self._skip_reason:
            if reason:
                self._wipe()
            self._skip_reason = reason
        if reason:
            self.counters["skipped"] += 1
            return
        if self.clock() < self._backoff_until:
            return
        if self._backend is None:
            self._backend = self.backend_factory()
            self._recheck = True
        self._check_monitors()
        want = cfg_get(self.cfg, "screen_watch_monitors")
        mons = sorted(self._layout)
        if want != "all":
            wanted = set(want if isinstance(want, (list, tuple, set)) else [want])
            mons = [m for m in mons if m in wanted]      # a configured monitor that vanished is skipped quietly
        for mon in mons:
            try:
                frame = self._backend.grab(mon)
            except Exception as e:
                self.counters["errors"] += 1
                pm = self.per_monitor.setdefault(mon, {"grabs": 0, "errors": 0, "last_error": ""})
                pm["errors"] += 1
                pm["last_error"] = type(e).__name__     # class name only (status file: no screen content)
                self._note_fail(mon, e)
                self._recheck = True
                self._backoff_until = self.clock() + self.GRAB_BACKOFF_S
                return
            self._note_ok(mon)
            self.counters["grabs"] += 1
            self.per_monitor.setdefault(mon, {"grabs": 0, "errors": 0, "last_error": ""})["grabs"] += 1
            det = self.detectors.get(mon)
            if det is None:
                det = self.detectors[mon] = ChangeDetector(float(cfg_get(self.cfg, "screen_watch_threshold")),
                                                           int(cfg_get(self.cfg, "screen_watch_level")),
                                                           int(cfg_get(self.cfg, "screen_watch_settle")),
                                                           float(cfg_get(self.cfg, "screen_watch_moving_keep_s")))
            ev = det.feed(sample(frame), self.clock())
            if ev is None:
                continue
            ev["monitor"] = mon
            if ev.get("moving"):                         # ring only: not a settled change (screen text ignores it)
                self.counters["moving_keeps"] = self.counters.get("moving_keeps", 0) + 1
            else:
                self.counters["events"] += 1
                self.changes.append(ev)
                self.rate.note_change()
            self.ring.push(mon, encode_jpeg(frame, int(cfg_get(self.cfg, "screen_watch_max_width")),
                                            int(cfg_get(self.cfg, "screen_watch_jpeg_quality"))),
                           {"fraction": ev["fraction"]})

    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:
                    self.counters["errors"] += 1
                    self.log("watch", f"screen watch tick failed: {type(e).__name__}")
                    if self._backend is not None:
                        self._backend.close()
                        self._backend = None
                    self._recheck = True
                    self._sleep(5)
                self._wake.clear()
                self._sleep(self.rate.interval())
        finally:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
