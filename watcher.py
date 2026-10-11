"""
watcher.py - proactive triggers for the claude2claude voice assistant.

The watcher OBSERVES and QUEUES PROMPTS. It never sends, posts or deletes anything, and it makes no model
call while polling. When a trigger trips, and only when the assistant is idle and it is not quiet hours,
the trigger's prompt is handed to the assistant through the `submit` callback as a system-originated turn.

Trigger types (watches.yaml, a list under `watches:`):
  timer   time: "17:30"            daily (optional date: 2026-10-12, weekdays: [mon, tue], once: true)
          every_min: 30 / every_s: 90   repeating interval
  file    folder, glob, mode: new_file | contains, pattern (regex, for contains), recursive
  screen  monitor: left|middle|right|primary|1..n, mode: diff | colour
          diff:   grid, cell_threshold, min_changed (fraction of grid cells that changed)
          colour: colour [r,g,b], tolerance, min_fraction, region [x0,y0,x1,y1] as 0..1 fractions

Common fields: name, type, enabled (default true), cooldown_s, prompt, params (or put params at top level).

Privacy: state and logs live in state/watcher/ only (gitignored). Never write under memory/topics or any synced
folder. Screen frames are compared in memory and never saved.
"""
import re
import json
import shutil
import pathlib
import datetime as dt
import threading

HERE = pathlib.Path(__file__).resolve().parent
STATE_DIR = HERE / "state" / "watcher"
DEFAULT_WATCHES = STATE_DIR / "watches.yaml"
EXAMPLE_WATCHES = HERE / "watches.example.yaml"

UTC = dt.timezone.utc
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TYPES = ("timer", "file", "screen")


# ------------------------------------------------------------------ UK time (stdlib only, no tzdata needed)
def _last_sunday(year, month):
    d = dt.date(year, month, 31)
    return d - dt.timedelta(days=(d.weekday() - 6) % 7)


class _London(dt.tzinfo):
    """Europe/London: BST from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last Sunday of October."""

    def _edges_utc(self, year):
        return (dt.datetime.combine(_last_sunday(year, 3), dt.time(1)),
                dt.datetime.combine(_last_sunday(year, 10), dt.time(1)))

    def utcoffset(self, d):
        start, end = self._edges_utc(d.year)
        n = d.replace(tzinfo=None)
        one = dt.timedelta(hours=1)
        # spring gap 01:00-02:00 local does not exist: treated as GMT (fold=0 convention), so it normalises forward
        if start + one <= n < end:
            return one                      # clearly BST (also the first pass through 01:00-02:00 in October)
        if end <= n < end + one and not d.fold:
            return one
        return dt.timedelta(0)

    def dst(self, d):
        return self.utcoffset(d)

    def tzname(self, d):
        return "BST" if self.utcoffset(d) else "GMT"

    def fromutc(self, d):
        u = d.replace(tzinfo=None)
        start, end = self._edges_utc(u.year)
        one = dt.timedelta(hours=1)
        if start <= u < end:
            return (u + one).replace(tzinfo=self)
        return u.replace(tzinfo=self, fold=1 if end <= u < end + one else 0)


LONDON = _London()


def uk(d):
    """Any aware datetime -> UK local time."""
    return d.astimezone(LONDON)


def uk_wall(date, hh, mm):
    """Aware UK datetime for a wall-clock time on a date. A time that does not exist (spring-forward gap) moves
    forward to the first real time after it; an ambiguous time (autumn) uses the first occurrence."""
    cand = dt.datetime(date.year, date.month, date.day, hh, mm, tzinfo=LONDON)
    back = cand.astimezone(UTC).astimezone(LONDON)
    if back.replace(tzinfo=None) != cand.replace(tzinfo=None):
        return back
    return cand


def parse_hhmm(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad time {s!r}, want HH:MM")
    return int(m.group(1)), int(m.group(2))


def next_daily(now, hh, mm, weekdays=None, date=None):
    """First UK wall-clock occurrence of hh:mm strictly after `now` (aware). Optional weekday filter or fixed date."""
    base = uk(now).date()
    if date is not None:
        t = uk_wall(date, hh, mm)
        return t if t > now else None
    for i in range(0, 9):
        d = base + dt.timedelta(days=i)
        if weekdays and WEEKDAYS[d.weekday()] not in weekdays:
            continue
        t = uk_wall(d, hh, mm)
        if t > now:
            return t
    return None


def in_quiet_hours(now, spec):
    """spec 'HH:MM-HH:MM' (UK time, may wrap midnight), '' = never quiet. Bad spec = never quiet."""
    if not spec:
        return False
    try:
        a, b = str(spec).split("-")
        (ah, am), (bh, bm) = parse_hhmm(a), parse_hhmm(b)
    except Exception:
        return False
    t = uk(now)
    cur, lo, hi = t.hour * 60 + t.minute, ah * 60 + am, bh * 60 + bm
    if lo == hi:
        return False
    return lo <= cur < hi if lo < hi else (cur >= lo or cur < hi)


# ------------------------------------------------------------------ screen maths (local, cheap, no model)
def _grid_gray(img, grid):
    import numpy as np
    from PIL import Image
    gw, gh = grid
    return np.asarray(img.convert("L").resize((gw, gh), Image.BOX), dtype="float32")


def screen_diff_fraction(prev, cur, grid=(32, 18), cell_threshold=24):
    """Fraction of grid cells whose average brightness moved by more than cell_threshold (0..255)."""
    import numpy as np
    a, b = _grid_gray(prev, grid), _grid_gray(cur, grid)
    return float((np.abs(a - b) > cell_threshold).mean())


def colour_fraction(img, colour, tolerance=40, region=(0, 0, 1, 1), max_side=160):
    """Fraction of pixels in a fractional region within `tolerance` per channel of `colour`."""
    import numpy as np
    w, h = img.size
    x0, y0, x1, y1 = region
    box = (int(w * x0), int(h * y0), max(int(w * x1), int(w * x0) + 1), max(int(h * y1), int(h * y0) + 1))
    c = img.convert("RGB").crop(box)
    s = max(c.size) / max_side
    if s > 1:
        c = c.resize((max(1, int(c.size[0] / s)), max(1, int(c.size[1] / s))))
    a = np.asarray(c, dtype="int16")
    ok = (np.abs(a - np.array(colour, dtype="int16")) <= tolerance).all(axis=2)
    return float(ok.mean())


def monitor_rects():
    """Monitor rectangles (l, t, r, b) sorted left to right. Empty list if pywin32 is unavailable."""
    try:
        import win32api
        rects = [m[2] for m in win32api.EnumDisplayMonitors()]
    except Exception:
        return []
    return sorted(rects, key=lambda r: (r[0], r[1]))


def pick_monitor(rects, name):
    """left / middle / right / primary / 1-based number -> rectangle, or None if there is no such monitor."""
    if not rects:
        return None
    n = str(name).strip().lower()
    if n.isdigit():
        i = int(n) - 1
        return rects[i] if 0 <= i < len(rects) else None
    if n == "left":
        return rects[0]
    if n == "right":
        return rects[-1] if len(rects) > 1 else None
    if n == "middle":
        return rects[len(rects) // 2] if len(rects) >= 3 else None
    if n == "primary":
        return next((r for r in rects if r[0] == 0 and r[1] == 0), rects[0])
    return None


def grab_monitor(name, rects=None):
    """Cheap screenshot of a named monitor as a PIL image (never saved). None on any failure."""
    try:
        from PIL import ImageGrab
        r = pick_monitor(rects if rects is not None else monitor_rects(), name)
        return ImageGrab.grab(bbox=r, all_screens=True) if r else None
    except Exception:
        return None


# ------------------------------------------------------------------ triggers
class WatchError(ValueError):
    pass


class Trigger:
    def __init__(self, name, kind, params, prompt, cooldown_s=60.0, enabled=True, quiet_exempt=False):
        self.name, self.kind, self.params, self.prompt = name, kind, dict(params), prompt
        self.cooldown_s, self.enabled, self.quiet_exempt = float(cooldown_s), bool(enabled), bool(quiet_exempt)
        self.last_fired = 0.0           # epoch seconds
        self.pending = None             # (detail, epoch) when tripped but not yet delivered
        self._rt = {}                   # per-type runtime state
        self.validate()

    def key(self):
        return json.dumps([self.kind, self.params, self.prompt], sort_keys=True, default=str)

    # -- validation, so bad entries are rejected at load and not at 3am
    def validate(self):
        p = self.params
        if self.kind == "timer":
            if "time" in p:
                parse_hhmm(p["time"])
                if "date" in p:
                    self._date()
                wd = p.get("weekdays")
                if wd is not None and (not isinstance(wd, list) or any(str(x).lower()[:3] not in WEEKDAYS for x in wd)):
                    raise WatchError("weekdays must be a list like [mon, tue]")
            elif "every_s" in p or "every_min" in p:
                if self._interval() <= 0:
                    raise WatchError("interval must be positive")
            else:
                raise WatchError("timer needs time: or every_min:/every_s:")
        elif self.kind == "file":
            if not p.get("folder"):
                raise WatchError("file watch needs folder")
            if p.get("mode", "new_file") not in ("new_file", "contains"):
                raise WatchError("file mode must be new_file or contains")
            if p.get("mode") == "contains":
                if not p.get("pattern"):
                    raise WatchError("contains mode needs pattern")
                try:
                    re.compile(p["pattern"])
                except re.error as e:
                    raise WatchError(f"bad regex: {e}")
        elif self.kind == "screen":
            if not p.get("monitor"):
                raise WatchError("screen watch needs monitor")
            mode = p.get("mode", "diff")
            if mode not in ("diff", "colour"):
                raise WatchError("screen mode must be diff or colour")
            if mode == "colour":
                c = p.get("colour")
                if not (isinstance(c, list) and len(c) == 3 and all(isinstance(v, (int, float)) for v in c)):
                    raise WatchError("colour mode needs colour: [r, g, b]")
            g = p.get("grid", [32, 18])
            if not (isinstance(g, list) and len(g) == 2 and all(isinstance(v, int) and v > 0 for v in g)):
                raise WatchError("grid must be [w, h]")

    def _date(self):
        try:
            d = self.params["date"]
            return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
        except ValueError:
            raise WatchError(f"bad date {self.params['date']!r}")

    def _interval(self):
        p = self.params
        try:
            return float(p["every_s"]) if "every_s" in p else float(p["every_min"]) * 60
        except (TypeError, ValueError):
            raise WatchError("interval must be a number")

    # -- polling: returns a detail string when tripped, else None
    def poll(self, now, ctx):
        return getattr(self, "_poll_" + self.kind)(now, ctx)

    def _poll_timer(self, now, ctx):
        rt, p = self._rt, self.params
        if "due" not in rt:
            if "time" in p:
                wd = [str(x).lower()[:3] for x in p["weekdays"]] if p.get("weekdays") else None
                if "date" in p:             # fixed date: due even if already slightly past (grace decides)
                    rt["due"] = uk_wall(self._date(), *parse_hhmm(p["time"]))
                else:
                    rt["due"] = next_daily(now, *parse_hhmm(p["time"]), weekdays=wd)
            else:
                rt["due"] = now + dt.timedelta(seconds=self._interval())
        due = rt["due"]
        if due is None or now < due:
            return None
        late = (now - due).total_seconds()
        if "time" in p:
            if p.get("once") or "date" in p:
                rt["due"] = None
                self.enabled = False
            else:
                wd = [str(x).lower()[:3] for x in p["weekdays"]] if p.get("weekdays") else None
                rt["due"] = next_daily(now, *parse_hhmm(p["time"]), weekdays=wd)
            if late > float(p.get("grace_s", 600)):
                return None                 # missed it (asleep / quiet hours / app was off): do not nag late
            return f"scheduled for {uk(due):%H:%M}"
        rt["due"] = now + dt.timedelta(seconds=self._interval())
        return f"every {self._interval() / 60:g} minutes"

    def _poll_file(self, now, ctx):
        p, rt = self.params, self._rt
        folder = pathlib.Path(str(p["folder"])).expanduser()
        if not folder.is_dir():
            return None
        pat = p.get("glob", "*")
        it = folder.rglob(pat) if p.get("recursive") else folder.glob(pat)
        cur = {}
        for f in it:
            try:
                if f.is_file():
                    s = f.stat()
                    cur[str(f)] = (s.st_mtime_ns, s.st_size)
            except OSError:
                pass
        prev = rt.get("seen")
        rt["seen"] = cur
        if prev is None:
            return None                     # first look is the baseline, never a trip
        if p.get("mode", "new_file") == "new_file":
            new = sorted(set(cur) - set(prev))
            return f"new file: {pathlib.Path(new[0]).name}" + (f" (+{len(new) - 1} more)" if len(new) > 1 else "") if new else None
        rx = re.compile(p["pattern"])
        cap = int(p.get("max_bytes", 1_000_000))
        for path, sig in cur.items():
            if prev.get(path) == sig:
                continue
            try:
                with open(path, "rb") as fh:
                    old = prev.get(path)
                    size = sig[1]
                    start = old[1] if old and size >= old[1] else 0      # appended data only, else whole file
                    fh.seek(max(start, size - cap))
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            m = rx.search(text)
            if m:
                return f"pattern found in {pathlib.Path(path).name}: {m.group(0)[:80]}"
        return None

    def _poll_screen(self, now, ctx):
        p, rt = self.params, self._rt
        img = ctx["capture"](p["monitor"])
        if img is None:
            rt["errors"] = rt.get("errors", 0) + 1
            return None
        if p.get("mode", "diff") == "colour":
            frac = colour_fraction(img, p["colour"], p.get("tolerance", 40), tuple(p.get("region", (0, 0, 1, 1))))
            hit = frac >= float(p.get("min_fraction", 0.05))
            was = rt.get("hit", False)
            rt["hit"] = hit
            if hit and (was is False or not p.get("on_enter", True)):
                return f"colour match {frac:.0%} on the {p['monitor']} screen"
            return None
        prev, rt["img"] = rt.get("img"), img
        if prev is None or prev.size != img.size:
            return None
        frac = screen_diff_fraction(prev, img, tuple(p.get("grid", (32, 18))), p.get("cell_threshold", 24))
        if frac >= float(p.get("min_changed", 0.03)):
            return f"{frac:.0%} of the {p['monitor']} screen changed"
        return None


# ------------------------------------------------------------------ loading
def parse_watches(path):
    """Read a watches file. Returns (triggers, errors). Never raises: a malformed file gives ([], [reason]) and a
    bad entry is skipped with a reason while the good ones load."""
    path = pathlib.Path(path)
    errors, out = [], []
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            import yaml
            data = yaml.safe_load(text)
    except Exception as e:
        return [], [f"cannot read {path.name}: {e}"]
    items = data.get("watches") if isinstance(data, dict) else data
    if items is None and isinstance(data, dict):
        items = []
    if not isinstance(items, list):
        return [], [f"{path.name}: expected a list of watches"]
    seen = set()
    for i, w in enumerate(items):
        label = f"entry {i + 1}"
        try:
            if not isinstance(w, dict):
                raise WatchError("not a mapping")
            name = str(w.get("name") or "").strip()
            if not name:
                raise WatchError("missing name")
            label = name
            if name in seen:
                raise WatchError("duplicate name")
            kind = str(w.get("type", "")).lower()
            if kind not in TYPES:
                raise WatchError(f"type must be one of {', '.join(TYPES)}")
            prompt = str(w.get("prompt") or "").strip()
            if not prompt:
                raise WatchError("missing prompt")
            params = dict(w.get("params") or {})
            for k, v in w.items():
                if k not in ("name", "type", "enabled", "cooldown_s", "prompt", "params", "quiet_exempt"):
                    params.setdefault(k, v)
            cd = w.get("cooldown_s", 60)
            if isinstance(cd, bool) or not isinstance(cd, (int, float)) or cd < 0:
                raise WatchError("cooldown_s must be a number >= 0")
            seen.add(name)
            out.append(Trigger(name, kind, params, prompt, cd, w.get("enabled", True) is not False,
                               w.get("quiet_exempt", False)))
        except (WatchError, ValueError, TypeError) as e:
            errors.append(f"{label}: {e}")
    return out, errors


# ------------------------------------------------------------------ the watcher
class Watcher:
    """Polls triggers; delivers at most one fired trigger per tick, only when the assistant is idle.

    is_idle() -> bool        assistant not working, not speaking, the user not talking
    submit(prompt, name, detail) -> bool   queue a system-originated turn; False = not accepted, try later
    """

    def __init__(self, path=None, is_idle=lambda: True, submit=lambda prompt, name, detail: True,
                 quiet_hours="", poll_s=5.0, max_pending_s=900.0, clock=None, capture=None,
                 state_dir=None, enabled=True):
        self.path = pathlib.Path(path) if path else DEFAULT_WATCHES
        self.state_dir = pathlib.Path(state_dir) if state_dir else STATE_DIR
        self.is_idle, self.submit = is_idle, submit
        self.quiet_hours, self.poll_s, self.max_pending_s = quiet_hours, float(poll_s), float(max_pending_s)
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.capture = capture or grab_monitor
        self.enabled = enabled
        self.triggers, self.errors = [], []
        self._mtime = None
        self._fired = {}                # name -> epoch, persisted so cooldowns survive restarts
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._load_fired()
        self.reload(force=True)

    # -- state and logs (state/watcher only)
    def _log(self, msg):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            f = self.state_dir / "watcher.log"
            if f.exists() and f.stat().st_size > 256_000:
                f.replace(self.state_dir / "watcher.old.log")
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"{uk(self.clock()):%Y-%m-%d %H:%M:%S} {msg}\n")
        except OSError:
            pass

    def _load_fired(self):
        try:
            self._fired = {k: float(v) for k, v in json.loads(
                (self.state_dir / "state.json").read_text(encoding="utf-8")).get("last_fired", {}).items()}
        except Exception:
            self._fired = {}

    def _save_fired(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_dir / "state.json.tmp"
            tmp.write_text(json.dumps({"last_fired": self._fired}), encoding="utf-8")
            tmp.replace(self.state_dir / "state.json")
        except OSError:
            pass

    def reload(self, force=False):
        """(Re)load the watches file if it changed. A broken file keeps the last good set running."""
        with self._lock:
            if not self.path.exists() and self.path == DEFAULT_WATCHES and EXAMPLE_WATCHES.exists():
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(EXAMPLE_WATCHES, self.path)
                except OSError:
                    pass
            try:
                m = self.path.stat().st_mtime_ns
            except OSError:
                m = None
            if not force and m == self._mtime:
                return
            self._mtime = m
            new, errs = parse_watches(self.path)
            if errs and not new and self.triggers:
                self.errors = errs
                self._log("watches file rejected, keeping previous set: " + "; ".join(errs))
                return
            old = {t.name: t for t in self.triggers}
            for t in new:
                o = old.get(t.name)
                if o and o.key() == t.key():
                    t._rt, t.pending = o._rt, o.pending
                t.last_fired = self._fired.get(t.name, 0.0)
            self.triggers, self.errors = new, errs
            for e in errs:
                self._log("watches problem: " + e)
            self._log(f"loaded {len(new)} watches ({sum(t.enabled for t in new)} enabled)")

    # -- one pass
    def tick(self, now=None):
        """Evaluate triggers and deliver at most one pending prompt. Returns the names delivered."""
        now = now or self.clock()
        if not self.enabled:
            return []
        self.reload()
        epoch = now.timestamp()
        quiet = in_quiet_hours(now, self.quiet_hours)
        with self._lock:
            if not quiet:
                for t in self.triggers:
                    if not t.enabled or t.pending or epoch - t.last_fired < t.cooldown_s:
                        continue
                    try:
                        detail = t.poll(now, {"capture": self.capture})
                    except Exception as e:      # a broken trigger must never take the watcher down
                        self._log(f"{t.name}: poll failed: {e}")
                        continue
                    if detail:
                        t.pending = (detail, epoch)
                        self._log(f"{t.name}: tripped ({detail})")
            for t in self.triggers:             # drop stale
                if t.pending and epoch - t.pending[1] > self.max_pending_s:
                    self._log(f"{t.name}: pending prompt expired undelivered")
                    t.pending = None
            ready = sorted((t for t in self.triggers if t.pending and (not quiet or t.quiet_exempt)),
                           key=lambda t: t.pending[1])
            if not ready or not self.is_idle():
                return []
            t = ready[0]
            detail = t.pending[0]
            prompt = t.prompt.replace("{detail}", detail).replace("{name}", t.name)
            try:
                ok = self.submit(prompt, t.name, detail)
            except Exception as e:
                self._log(f"{t.name}: submit failed: {e}")
                ok = False
            if not ok:
                return []
            t.pending, t.last_fired = None, epoch
            self._fired[t.name] = epoch
            self._save_fired()
            self._log(f"{t.name}: delivered")
            return [t.name]

    # -- background thread
    def start(self):
        self.enabled = True
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.poll_s):
                try:
                    self.tick()
                except Exception as e:
                    self._log(f"tick failed: {e}")

        self._thread = threading.Thread(target=loop, name="watcher", daemon=True)
        self._thread.start()
        self._log("watcher started")

    def stop(self):
        self.enabled = False
        self._stop.set()
        self._log("watcher stopped")


if __name__ == "__main__":          # python watcher.py [file]  -> validate a watches file
    import sys
    f = sys.argv[1] if len(sys.argv) > 1 else (DEFAULT_WATCHES if DEFAULT_WATCHES.exists() else EXAMPLE_WATCHES)
    ts, es = parse_watches(f)
    print(f"{f}: {len(ts)} valid ({sum(t.enabled for t in ts)} enabled)")
    for e in es:
        print("  problem:", e)
    for t in ts:
        print(f"  {'on ' if t.enabled else 'off'} {t.name} [{t.kind}] cooldown {t.cooldown_s:g}s")
