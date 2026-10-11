"""Live screen commentator (first version).

Every `live_commentary_interval_s` it takes the newest frame from the screen watcher's RAM ring, skips the look
when nothing has meaningfully changed since the last one, and otherwise sends that one frame to a separate,
stateless Haiku call (claude -p, never the main session, no session file, no tools). The model answers SILENT or
one short AIDA line, which is spoken through the normal speech path when the user isn't talking and no turn is running.

Privacy: frames stay in RAM. The only copy that leaves the machine is the single JPEG sent with a look. Nothing is
written to disk here; the lines said are kept in RAM only (last few, for repeat suppression).
"""
import base64
import difflib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

DEFAULTS = {
    "live_commentary_enabled": False,       # The user turns it on by voice: "commentary on"
    "live_commentary_interval_s": 20,       # one look at most this often
    "live_commentary_min_gap_s": 45,        # least time between two spoken lines (deaths may cut in after 15 s)
    "live_commentary_death_gap_s": 15,
    "live_commentary_death_cooldown_s": 120,  # after a death line, no further death lines until a not-dead look or this long
    "live_commentary_max_per_hour": 120,    # hard caps on model calls (looks), not on spoken lines
    "live_commentary_max_per_day": 800,
    "live_commentary_model": "claude-haiku-5-5",
    "live_commentary_timeout_s": 30,
    "live_commentary_max_words": 15,
    "live_commentary_monitors": "all",      # "all" or a list of monitor numbers to consider
}

SYSTEM_PROMPT = (
    "You are AIDA, a deadpan, dry, slightly cocky British ex-military AI watching the user's screen over his shoulder. "
    "You get one screenshot plus the last things you said. Reply with exactly SILENT unless something is genuinely "
    "worth a remark: his game character dying, a win, a fail, an error dialog, something plainly daft or funny. Most "
    "looks should be SILENT. If you do speak: one line, at most 15 words, British English, spoken aloud, no emojis, no "
    "markdown, no quotes, no exclamation marks. NEVER invent or guess anything. Only mention what is clearly and "
    "plainly visible on screen; never speculate about causes, places, enemies or what happened. "
    "If his character has just died in a game (death screen, 'You died', release spirit, ghost, corpse run), start "
    "the reply with DEATH: and then give ONLY a short laugh or jibe at the fact that he died, for example 'Ha ha, you "
    "died, sucker.' Vary the wording, rude is fine. For a death do NOT say or guess how, where, why or to what he "
    "died: no cause, no location, no enemy, no circumstances, no advice. Only the death itself. Never "
    "repeat or closely rephrase anything you said before. Never read out passwords, emails, names, or personal or "
    "customer details; if the screen shows private or work data, reply SILENT."
)


def cfg_get(cfg, key):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


def parse_reply(text, max_words=15):
    """Model text -> (line or None, is_death). SILENT / empty / junk -> (None, False)."""
    t = (text or "").strip().strip('"').strip()
    if not t or re.match(r"^\W*silent\W*$", t, re.I) or t.upper().startswith("SILENT"):
        return None, False
    death = False
    m = re.match(r"^\W*death\s*:\s*", t, re.I)
    if m:
        death, t = True, t[m.end():]
    t = re.sub(r"[*_#`]", "", t.splitlines()[0]).strip()
    if not t:
        return None, False
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip(",;:") + "."
    return t, death


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def is_repeat(line, recent, ratio=0.75):
    n = _norm(line)
    return any(n == _norm(r) or difflib.SequenceMatcher(None, n, _norm(r)).ratio() >= ratio for r in recent)


def fit_jpeg(jpeg, max_w=960, max_h=540, quality=60):
    """Downscale a JPEG (in RAM) to fit max_w x max_h. Returns the input unchanged when it already fits."""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg))
    if img.width <= max_w and img.height <= max_h:
        return jpeg
    img.thumbnail((max_w, max_h))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue()


class ClaudeLook:
    """One stateless Haiku call per look: claude -p, stream-json input with the image inline, no session file,
    no MCP, no tools, run from a folder outside the project so no CLAUDE.md or memory is loaded."""

    def __init__(self, claude, model, timeout_s=30, log=None):
        self.claude, self.model, self.timeout_s = claude, model, timeout_s
        self.log = log or (lambda who, text: None)
        # Temp, not LOCALAPPDATA: Store Python virtualises AppData\Local writes, so claude.exe could not see the folder.
        self.cwd = Path(tempfile.gettempdir()) / "aida_watch"

    def __call__(self, jpeg, user_text):
        self.cwd.mkdir(parents=True, exist_ok=True)
        msg = {"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpeg).decode("ascii")}},
            {"type": "text", "text": user_text}]}}
        cmd = [self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", self.model, "--system-prompt", SYSTEM_PROMPT, "--no-session-persistence",
               "--strict-mcp-config", "--tools", "", "--disable-slash-commands", "--setting-sources", "",
               "--effort", "low"]
        # Slim context (measured 11 Oct 2026): no tool definitions, skills, settings, plugins or CLAUDE.md cut a look
        # from ~20k to ~1.6k input tokens; no thinking keeps output small. (--bare needs an API key, so not used.)
        env = dict(os.environ, MAX_THINKING_TOKENS="0")
        r = subprocess.run(cmd, cwd=str(self.cwd), input=json.dumps(msg) + "\n", capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace", timeout=self.timeout_s,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = None
        for line in (r.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                if ev.get("is_error"):
                    raise RuntimeError(str(ev.get("result") or ev.get("subtype"))[:200])
                result = ev.get("result", "")
        if result is None:
            raise RuntimeError(f"no result (exit {r.returncode}): {(r.stderr or '')[:200]}")
        return result


class LiveCommentary:
    """Background commentator. All collaborators are injected so tests can drive tick() directly.

    watch:    object with get_latest_frame(monitor) -> {"ts", "jpeg", ...} | None and monitor_rects()
    look:     callable(jpeg_bytes, user_text) -> model text
    say:      callable(line) -> speaks it (the app's normal speech path)
    show:     callable(line) -> puts it in the voice window log
    muted_fn: -> bool;  busy_fn: -> reason string ('' when free: The user not talking, no turn, not speaking)
    """

    def __init__(self, cfg, watch, look, say, show=None, muted_fn=None, busy_fn=None, log=None,
                 clock=time.monotonic, wall=time.time):
        self.cfg = dict(cfg or {})
        self.watch, self.look, self.say = watch, look, say
        self.show = show or (lambda line: None)
        self.muted_fn = muted_fn or (lambda: False)
        self.busy_fn = busy_fn or (lambda: "")
        self.log = log or (lambda who, text: None)
        self.clock, self.wall = clock, wall
        self.enabled = bool(cfg_get(self.cfg, "live_commentary_enabled"))
        self.recent = deque(maxlen=5)          # last lines said (RAM only)
        self.calls = deque()                   # clock() of each model call in the last day
        self.last_look = None                  # clock() of the last look attempt
        self.last_frame_ts = {}                # monitor -> ring ts already looked at
        self.last_spoken = None
        self.last_death_spoken = None          # clock() of the last death line; None once a not-dead frame is seen
        self.counters = {"looks": 0, "lines": 0, "silent": 0, "skipped_nochange": 0, "skipped_busy": 0,
                         "skipped_cap": 0, "skipped_gap": 0, "repeats": 0, "errors": 0}
        self._cap_logged = None
        self.reasons = {}                      # decision -> count since the last summary line
        self.last_reason, self.last_busy, self._summary_at = "", "", None
        self._stop = threading.Event()
        self._thread = None

    # ---- control
    def set_enabled(self, flag):
        self.enabled = bool(flag)
        if self.enabled:
            self.last_look = None
            self.start()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live_commentary", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.enabled:
                    self.tick()
            except Exception as e:
                self.counters["errors"] += 1
                self.log("commentary", f"commentary tick failed: {type(e).__name__}: {str(e)[:200]}")
            self._stop.wait(2.0)

    # ---- helpers
    def _interval(self):
        return float(cfg_get(self.cfg, "live_commentary_interval_s"))

    def _cap_ok(self):
        now = self.clock()
        while self.calls and now - self.calls[0] > 86400:
            self.calls.popleft()
        hour = sum(1 for t in self.calls if now - t <= 3600)
        if hour >= int(cfg_get(self.cfg, "live_commentary_max_per_hour")):
            return "hourly"
        if len(self.calls) >= int(cfg_get(self.cfg, "live_commentary_max_per_day")):
            return "daily"
        return ""

    def _pick_frame(self):
        """Newest frame across the allowed monitors that is newer than the last one looked at, else None."""
        want = cfg_get(self.cfg, "live_commentary_monitors")
        try:
            mons = sorted(self.watch.monitor_rects()) or [1]
        except Exception:
            mons = [1]
        if want != "all":
            allowed = set(want if isinstance(want, (list, tuple, set)) else [want])
            mons = [m for m in mons if m in allowed]
        best = None
        for m in mons:
            f = self.watch.get_latest_frame(m)
            if not f or f.get("ts") is None:
                continue
            if f["ts"] <= self.last_frame_ts.get(m, float("-inf")):
                continue                              # the watcher only keeps a frame on a meaningful change
            if best is None or f["ts"] > best["ts"]:
                best = f
        return best

    def _user_text(self):
        said = "\n".join(f"- {r}" for r in self.recent) or "- (nothing yet)"
        return (f"Time {time.strftime('%H:%M')}. Things you already said (do not repeat):\n{said}\n"
                "Reply SILENT, or one short line, or DEATH: line if his character just died.")

    # ---- one pass
    SUMMARY_EVERY_S = 30.0    # decision summary in the program log at most this often

    def tick(self):
        """Returns a short reason string (for tests and logs). Also keeps a rate-limited decision summary."""
        reason = self._tick()
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if reason not in ("interval", "off"):
            self.last_reason = reason
        now = self.clock()
        if self._summary_at is None:
            self._summary_at = now
            self.log("commentary", "commentary running: first decision " + reason)
        elif now - self._summary_at >= self.SUMMARY_EVERY_S:
            self._summary_at = now
            parts = ", ".join(f"{k} {v}" for k, v in sorted(self.reasons.items()) if k != "interval")
            busy = f" ({self.last_busy})" if self.last_reason == "busy" and self.last_busy else ""
            self.log("commentary", f"commentary last {int(self.SUMMARY_EVERY_S)} s: {parts or 'nothing'}; "
                                   f"last decision {self.last_reason}{busy}; looks today {len(self.calls)}")
            self.reasons = {}
        return reason

    def _tick(self):
        if not self.enabled:
            return "off"
        now = self.clock()
        if self.last_look is not None and now - self.last_look < self._interval():
            return "interval"
        if self.muted_fn():
            return "muted"
        busy = self.busy_fn()
        if busy:
            self.last_busy = str(busy)
            self.counters["skipped_busy"] += 1
            return "busy"
        gap = float(cfg_get(self.cfg, "live_commentary_min_gap_s"))
        death_gap = float(cfg_get(self.cfg, "live_commentary_death_gap_s"))
        if self.last_spoken is not None and now - self.last_spoken < min(gap, death_gap):
            self.counters["skipped_gap"] += 1
            return "gap"                              # not even a death line could be spoken yet: don't pay
        frame = self._pick_frame()
        if frame is None:
            self.counters["skipped_nochange"] += 1
            self.last_look = now
            return "nochange"
        cap = self._cap_ok()
        if cap:
            self.counters["skipped_cap"] += 1
            if self._cap_logged != cap:
                self._cap_logged = cap
                self.log("commentary", f"commentary {cap} cap reached; no more looks until it clears")
            return "cap"
        self._cap_logged = None
        self.last_look = now
        self.last_frame_ts[frame["monitor"]] = frame["ts"]
        self.calls.append(now)
        self.counters["looks"] += 1
        t0 = self.clock()
        jpeg = None
        try:
            jpeg = fit_jpeg(frame["jpeg"])
            text = self.look(jpeg, self._user_text())
        except Exception as e:
            self.counters["errors"] += 1
            self.log("commentary", f"look failed: {type(e).__name__}: {str(e)[:200]}")
            return "error"
        finally:
            jpeg = None                               # drop our reference to the frame straight away
        took = self.clock() - t0
        line, death = parse_reply(text, int(cfg_get(self.cfg, "live_commentary_max_words")))
        what = "SILENT" if line is None else ("DEATH: " if death else "") + line
        self.log("commentary", f"commentary look (monitor {frame.get('monitor')}, {took:.1f} s): {what}")
        if not death:
            self.last_death_spoken = None             # frame judged not-dead: the next death counts afresh
        if line is None:
            self.counters["silent"] += 1
            return "silent"
        if death and self.last_death_spoken is not None and                 self.clock() - self.last_death_spoken < float(cfg_get(self.cfg, "live_commentary_death_cooldown_s")):
            self.counters["repeats"] += 1
            self.log("commentary", "commentary death line dropped: same death already reported")
            return "death_dup"
        if is_repeat(line, self.recent):
            self.counters["repeats"] += 1
            self.log("commentary", "commentary line dropped: repeat")
            return "repeat"
        now = self.clock()
        need = death_gap if death else gap
        if self.last_spoken is not None and now - self.last_spoken < need:
            self.counters["skipped_gap"] += 1
            self.log("commentary", "commentary line dropped: too soon after the last one")
            return "gap"
        busy = self.busy_fn() or ("muted" if self.muted_fn() else "")
        if busy:                                      # things changed during the call: drop it, never interrupt
            self.last_busy = str(busy)
            self.counters["skipped_busy"] += 1
            self.log("commentary", f"commentary line dropped: {busy}")
            return "busy"
        self.recent.append(line)
        self.last_spoken = now
        if death:
            self.last_death_spoken = now
            self.counters["deaths"] = self.counters.get("deaths", 0) + 1
        self.counters["lines"] += 1
        self.show(line)
        self.say(line)
        return "death" if death else "spoke"

    def status(self):
        return {"enabled": self.enabled, "calls_last_day": len(self.calls), **self.counters}


def parse_command(t):
    """'commentary on' / 'turn commentary off' / 'live commentary on' -> True/False, else None."""
    m = re.fullmatch(r"(?:ok |okay )?(?:turn |switch )?(?:the )?(?:live )?(?:commentary|commentator) (on|off)\W*",
                     (t or "").lower().strip())
    if not m:
        m = re.fullmatch(r"(?:ok |okay )?(?:turn |switch )(on|off) (?:the )?(?:live )?(?:commentary|commentator)\W*",
                         (t or "").lower().strip())
    return None if not m else m.group(1) == "on"
