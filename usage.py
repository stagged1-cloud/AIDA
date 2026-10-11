"""Usage bar data: how full the current conversation's context window is (no Tk, no network).

Measured: the last main-conversation assistant turn's input + cache-read + cache-creation + output tokens,
divided by the model's context window. Fed by the Claude Code stream-json events the Brain already receives;
falls back to the newest assistant turn in the session's transcript JSONL (numbers only, never text).

Window size, in order of trust: the CLI's own `result.modelUsage[model].contextWindow`, a "[1m]" suffix on the
model id, the config override `usage_window_tokens`, then 200k (flagged as assumed).

Plan usage (the 5-hour limit): `claude -p` stream-json emits a `rate_limit_event` once per reply, carrying
`rate_limit_info.unifiedWindows.five_hour` / `seven_day` {utilization, resetsAt}. Nothing is scraped and no
credentials are read. The last figure is persisted (numbers only) so a restart shows it until it is stale.
"""
import glob
import json
import os
import threading
import time

DEFAULT_WINDOW = 200_000
EXTENDED_WINDOW = 1_000_000           # used instead of the assumed 200k once usage proves the window is bigger
WARN_PCT, HIGH_PCT = 60.0, 85.0       # green below 60, amber 60-85, red above 85
NA = "n/a"


# ---------------------------------------------------------------- pure helpers
def window_for_model(model, reported=None, override=None):
    """Return (tokens, assumed). `reported` is {model_id: window} learned from the CLI."""
    model = model or ""
    reported = reported or {}
    if model in reported and reported[model]:
        return int(reported[model]), False
    for k, v in reported.items():                    # alias / suffix differences ("x" vs "x[1m]")
        if v and model and (k.startswith(model) or model.startswith(k)):
            return int(v), False
    if "[1m]" in model.lower():
        return 1_000_000, False
    if override:
        return int(override), False
    return DEFAULT_WINDOW, True


def tokens_from_usage(u):
    """Context tokens from one `usage` dict, or None if it carries no numbers."""
    if not isinstance(u, dict):
        return None
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    vals = [u.get(k) for k in keys]
    if all(not isinstance(v, (int, float)) or isinstance(v, bool) for v in vals):
        return None
    return int(sum(v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0))


def context_usage(used_tokens=None, model=None, window_tokens=None, source="stream", updated_at=None,
                  assumed=False):
    """The snapshot dict. Missing data gives percent None (the UI shows 'n/a'), never an exception."""
    ok = isinstance(used_tokens, (int, float)) and not isinstance(used_tokens, bool) and used_tokens >= 0
    win = window_tokens if isinstance(window_tokens, (int, float)) and window_tokens > 0 else DEFAULT_WINDOW
    if assumed and ok and used_tokens > win:
        win = EXTENDED_WINDOW        # a guessed 200k window cannot hold more than 200k: the real one is bigger
    return {
        "used_tokens": int(used_tokens) if ok else None,
        "window_tokens": int(win),
        "percent": round(used_tokens / win * 100.0, 1) if ok else None,
        "model": model or "",
        "source": source if ok else "none",
        "updated_at": updated_at if updated_at is not None else time.time(),
        "assumed_window": bool(assumed),
    }


def bar_fraction(percent):
    """0..1 for drawing; clamped (the real figure stays in the tooltip)."""
    if percent is None:
        return 0.0
    return max(0.0, min(1.0, percent / 100.0))


def level(percent):
    """'ok' / 'warn' / 'high' / 'na'."""
    if percent is None:
        return "na"
    if percent > HIGH_PCT:
        return "high"
    if percent >= WARN_PCT:
        return "warn"
    return "ok"


def fmt_tokens(n):
    if n is None:
        return NA
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(int(n))


def label_text(snap):
    p = snap.get("percent")
    return NA if p is None else f"{p:.0f}%"


def tooltip_text(snap, plan=None):
    p = snap.get("percent")
    if p is None:
        t = ("Context used: n/a (no usage figures yet). This is how full this conversation's memory is. "
             "Restart or 'start fresh' to clear it.")
    else:
        t = (f"Context used: {fmt_tokens(snap['used_tokens'])} of {fmt_tokens(snap['window_tokens'])} tokens "
             f"({p:.0f}%) — how full this conversation's memory is. Restart or 'start fresh' to clear it.")
        if p > 100:
            t += f" Over the window: the bar is clamped at 100%, real figure {p:.0f}%."
        if snap.get("assumed_window"):
            t += " Window size assumed until the first reply reports it."
    if plan and plan.get("percent") is not None:
        t += f"\nPlan limit ({plan['label']}): {plan['percent']:.0f}% used"
        if plan.get("resets_at"):
            t += f", resets {time.strftime('%H:%M', time.localtime(plan['resets_at']))}"
        t += "."
    if plan and plan.get("weekly_percent") is not None:
        t += f"\nWeekly: {plan['weekly_percent']:.0f}% used."
    return t


def plan_label_text(plan):
    """Main bar label: the plan session percentage, or n/a when Claude Code has not reported one."""
    p = plan.get("percent") if plan else None
    return NA if p is None else f"{p:.0f}%"


def session_tooltip_text(snap, plan=None):
    """Tooltip for the main 'Session' bar. Says exactly why when there is no number."""
    if plan and plan.get("percent") is not None:
        t = f"Session ({plan['label']}): {plan['percent']:.0f}% used"
        if plan.get("resets_at"):
            t += f", resets {time.strftime('%H:%M', time.localtime(plan['resets_at']))}"
        t += ". Reported by Claude Code itself."
    else:
        t = ("Session: n/a. No current 5-hour figure from Claude Code (its rate_limit_event arrives after each "
             "reply; none recent). No number is invented.")
    if plan and plan.get("weekly_percent") is not None:
        t += f"\nWeekly: {plan['weekly_percent']:.0f}% used."
    return t + "\n" + tooltip_text(snap)


# ---------------------------------------------------------------- transcript fallback
def default_projects_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def find_transcript(session_id, root=None):
    """Path of <session_id>.jsonl under the projects folder, or None."""
    if not session_id or any(c in str(session_id) for c in "/\\*?"):
        return None
    hits = glob.glob(os.path.join(root or default_projects_dir(), "*", f"{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def parse_assistant_line(line):
    """(model, used_tokens) from one transcript or stream line (str or dict) if it is a main-chain
    assistant turn with usage, else None. Only numbers and the model id are read."""
    if isinstance(line, (str, bytes)):
        try:
            d = json.loads(line)
        except ValueError:
            return None
    else:
        d = line
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    if d.get("isSidechain") or d.get("parent_tool_use_id"):
        return None
    m = d.get("message")
    if not isinstance(m, dict):
        return None
    model = m.get("model") or ""
    if model.startswith("<"):                       # "<synthetic>" placeholders carry no real usage
        return None
    used = tokens_from_usage(m.get("usage"))
    return (model, used) if used is not None else None


def read_transcript_usage(path, tail_bytes=600_000):
    """Latest main-chain assistant usage in a transcript file -> (model, used) or None. Reads the tail only."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None
    for raw in reversed(data.splitlines()):
        r = parse_assistant_line(raw)
        if r:
            return r
    return None


# ---------------------------------------------------------------- plan usage (rate_limit_event)
# Real shape (Claude Code 2.1.295, headless stream-json, one event per reply, between `assistant` and `result`):
#   {"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791664200,"rateLimitType":"five_hour",
#    "unifiedWindows":{"five_hour":{"utilization":0.8,"resetsAt":1791664200},
#                      "seven_day":{"utilization":0.1,"resetsAt":1792216800},"seven_day_overage_included":{...}}}}
# UNIT RULE: a number <= 1.0 is a fraction (0.79 -> 79%), > 1 is already a percent (79 -> 79%). Ambiguous at exactly
# 1.0: treated as 100% (a genuine "1%" sent as the bare number 1 would be misread).
_PLAN_LABELS = {"five_hour": "5-hour session", "seven_day": "weekly", "seven_day_opus": "weekly (Opus)",
                "seven_day_sonnet": "weekly (Sonnet)"}
PLAN_STALE_SECONDS = 6 * 3600           # a persisted figure older than this is shown as n/a
PLAN_FILE_KEYS = ("percent", "resets_at", "seen_at", "weekly_percent", "weekly_resets_at")


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def to_percent(u):
    """Utilisation (fraction or percent, see UNIT RULE) -> percent rounded to 0.1, or None if unusable."""
    u = _num(u)
    if u is None or u < 0:
        return None
    return round(u * 100.0 if u <= 1.0 else float(u), 1)


def find_rate_limit_info(ev, depth=4):
    """The `rate_limit_info` dict anywhere in an event (it may arrive under another type name)."""
    if depth < 0:
        return None
    if isinstance(ev, dict):
        v = ev.get("rate_limit_info")
        if isinstance(v, dict):
            return v
        if ev.get("type") in ("assistant", "user"):
            return None                                    # conversation events are never searched
        items = list(ev.values())
    elif isinstance(ev, list):
        items = ev[:50]
    else:
        return None
    for x in items:
        if isinstance(x, (dict, list)):
            r = find_rate_limit_info(x, depth - 1)
            if r:
                return r
    return None


def parse_rate_limit(ev, now=None):
    """Plan snapshot from a rate_limit_event, or None if it carries no utilisation at all.
    percent/resets_at: unifiedWindows.five_hour, else top-level utilization (unless the top-level type is a weekly
    one). weekly_percent/weekly_resets_at: unifiedWindows.seven_day. Either half may be None."""
    info = find_rate_limit_info(ev)
    if not isinstance(info, dict):
        return None
    wins = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    five = wins.get("five_hour") if isinstance(wins.get("five_hour"), dict) else {}
    week = wins.get("seven_day") if isinstance(wins.get("seven_day"), dict) else {}
    rtype = info.get("rateLimitType")
    pct, resets = to_percent(five.get("utilization")), _num(five.get("resetsAt"))
    wpct, wresets = to_percent(week.get("utilization")), _num(week.get("resetsAt"))
    top = to_percent(info.get("utilization"))
    if top is not None:
        if isinstance(rtype, str) and rtype.startswith("seven_day"):
            if wpct is None:
                wpct, wresets = top, _num(info.get("resetsAt"))
        elif pct is None:
            pct, resets = top, _num(info.get("resetsAt"))
    if pct is None and wpct is None:
        return None
    return {"percent": pct, "label": _PLAN_LABELS["five_hour"], "resets_at": resets,
            "weekly_percent": wpct, "weekly_resets_at": wresets,
            "status": info.get("status") if isinstance(info.get("status"), str) else None,
            "seen_at": now if now is not None else time.time()}


def merge_plan(old, new):
    """New event over the kept one: a half the new event lacks (e.g. weekly-only) survives from the old."""
    if not new:
        return old
    out = dict(new)
    if old:
        if out.get("percent") is None:
            for k in ("percent", "resets_at", "seen_at"):
                out[k] = old.get(k)
        if out.get("weekly_percent") is None:
            for k in ("weekly_percent", "weekly_resets_at"):
                out[k] = old.get(k)
    return out


def save_plan(path, plan):
    """Atomic write of numbers only (no conversation text). Never raises."""
    try:
        data = {k: plan.get(k) for k in PLAN_FILE_KEYS if _num(plan.get(k)) is not None}
        tmp = f"{path}.{os.getpid()}.tmp"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_plan(path, now=None):
    """Persisted plan snapshot, or None if missing / garbage / stale (seen > 6 h ago, or its window has reset)."""
    now = now if now is not None else time.time()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        seen = _num(d.get("seen_at"))
        if seen is None or now - seen > PLAN_STALE_SECONDS or seen > now + 300:
            return None
        p = {"percent": _num(d.get("percent")), "label": _PLAN_LABELS["five_hour"],
             "resets_at": _num(d.get("resets_at")), "weekly_percent": _num(d.get("weekly_percent")),
             "weekly_resets_at": _num(d.get("weekly_resets_at")), "status": None, "seen_at": seen}
        return _expire_plan(p, now)
    except Exception:
        return None


def _expire_plan(p, now):
    """Drop halves whose window has reset; None if nothing is left."""
    if not p:
        return None
    p = dict(p)
    if p.get("resets_at") and p["resets_at"] < now:
        p["percent"], p["resets_at"] = None, None
    if p.get("weekly_resets_at") and p["weekly_resets_at"] < now:
        p["weekly_percent"], p["weekly_resets_at"] = None, None
    return p if (p.get("percent") is not None or p.get("weekly_percent") is not None) else None


# ---------------------------------------------------------------- tracker (fed by the Brain)
class UsageTracker:
    def __init__(self, window_override=None, clock=time.time, plan_path=None):
        self._lock = threading.Lock()
        self._clock = clock
        self.plan_path = plan_path
        self.override = window_override
        self.windows = {}
        self.session = None
        self._reset_locked()
        if plan_path:
            self.plan = load_plan(plan_path, self._clock())      # recent value right after a restart

    def _reset_locked(self):
        self.model = ""
        self.used = None
        self.source = "none"
        self.updated = self._clock()
        self.plan = None

    def reset(self, session=None):
        """New conversation: back to zero / n/a. (Plan usage is account-wide, so it survives.)"""
        with self._lock:
            plan = self.plan
            self._reset_locked()
            self.plan = plan
            self.session = session

    def feed(self, ev):
        """One stream-json event. Returns True if the snapshot changed. Never raises."""
        try:
            return self._feed(ev)
        except Exception:
            return False

    def _feed(self, ev):
        if not isinstance(ev, dict):
            return False
        kind, sub = ev.get("type"), ev.get("subtype")
        with self._lock:
            sid = ev.get("session_id")
            if kind == "system" and sub == "init":
                # a changed session id is NOT a reset: `--resume` may mint a new id for the same memory.
                # Brain calls reset() itself when it really starts a new conversation.
                if sid:
                    self.session = sid
                if ev.get("model"):
                    self.model = ev["model"]
                return True
            if kind == "system" and sub in ("compact_boundary", "conversation_reset"):
                self.used = 0 if sub == "compact_boundary" else None    # next assistant turn sets the real figure
                self.updated = self._clock()
                return True
            if kind == "assistant":
                r = parse_assistant_line(ev)
                if r:
                    self.model, self.used = r
                    self.source = "stream"
                    self.updated = self._clock()
                    if sid:
                        self.session = sid
                    return True
                return False
            if kind == "result":
                mu = ev.get("modelUsage")
                if isinstance(mu, dict):
                    for k, v in mu.items():
                        w = v.get("contextWindow") if isinstance(v, dict) else None
                        if isinstance(w, (int, float)) and w > 0:
                            self.windows[k] = int(w)
                if "rate_limit" in json.dumps(ev, default=str):       # plan data riding on the result event
                    self._take_plan_locked(ev)
                return True
            if kind == "rate_limit_event" or (kind not in ("assistant", "user", "stream_event")
                                              and "rate_limit" in json.dumps(ev, default=str)):
                return self._take_plan_locked(ev)
            return False

    def _take_plan_locked(self, ev):
        p = parse_rate_limit(ev, self._clock())     # also finds it under another type name / nested
        if not p:
            return False
        self.plan = merge_plan(self.plan, p)
        if self.plan_path:
            save_plan(self.plan_path, self.plan)
        return True

    def load_transcript(self, session_id, root=None):
        """Fallback after a restart: seed from the session's transcript. Only if nothing has streamed yet."""
        path = find_transcript(session_id, root)
        r = read_transcript_usage(path) if path else None
        with self._lock:
            self.session = session_id or self.session
            if r and self.used is None:
                self.model, self.used = r
                self.source = "transcript"
                self.updated = self._clock()
                return True
        return False

    def snapshot(self):
        with self._lock:
            win, assumed = window_for_model(self.model, self.windows, self.override)
            return context_usage(self.used, self.model, win, self.source, self.updated, assumed)

    def plan_snapshot(self):
        """Plan usage dict, or None if never reported or its window has reset."""
        with self._lock:
            self.plan = p = _expire_plan(self.plan, self._clock())
            return dict(p) if p else None
