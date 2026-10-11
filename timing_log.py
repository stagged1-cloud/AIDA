"""Per-turn latency timing for the voice assistant. Pure and safe: no I/O except write(), which never raises.

One JSON line per turn goes to logs\\timing.jsonl. Numbers, flags and short labels only: never what was
said or replied. Read the breakdown with:  python timing_log.py [N]   (median / p90 per stage, last N turns)

Also holds resolve_effort(): the config `effort` setting -> the value for `claude --effort` (pure, testable).
"""
import json
import math
import pathlib
import sys
import threading
import time

EFFORT_ALLOWED = ("low", "medium", "high", "xhigh", "max")
EFFORT_DEFAULT = "low"
DEEP_EFFORT = "high"          # 'think hard' jobs always use this, whatever the config says

STAGES = ("stt_ms", "first_event_ms", "first_text_ms", "tool_ms_total", "claude_total_ms",
          "tts_first_audio_ms", "turn_total_ms")


def resolve_effort(value=EFFORT_DEFAULT, deep=False):
    """Config value -> effort string to pass to claude, or "" for 'do not pass the flag' (Claude Code default).
    deep=True ('think hard') always gives high. None / empty = Claude Code default. An unknown value is
    ignored (returns "") rather than handing claude a flag that would stop it starting."""
    if deep:
        return DEEP_EFFORT
    if value is None:
        return ""
    v = str(value).strip().lower()
    return v if v in EFFORT_ALLOWED else ""


def effort_args(value=EFFORT_DEFAULT, deep=False):
    e = resolve_effort(value, deep)
    return ["--effort", e] if e else []


def _ms(a, b):
    return None if a is None or b is None else int(round((b - a) * 1000))


class TurnTimer:
    """Marks named moments in one turn. Thread-safe, injectable clock, never raises from public methods.
    Marks are first-wins: marking the same name twice keeps the earlier time."""

    def __init__(self, kind="typed", model="", clock=time.time):
        self.clock = clock
        self.kind = "voice" if kind == "voice" else "typed"
        self.model = str(model or "")[:40]
        self.helper = False
        self._lock = threading.Lock()
        self._marks = {}
        self._tools = {}          # tool_use_id -> [start, end]
        self._written = False
        self._marks["start"] = clock()       # speech ended / message submitted

    def mark(self, name):
        try:
            with self._lock:
                self._marks.setdefault(name, self.clock())
        except Exception:
            pass

    def has(self, name):
        with self._lock:
            return name in self._marks

    def event(self, ev):
        """Feed one stream-json event. Only counted after the 'sent' mark (ignores stale late events)."""
        try:
            if not self.has("sent") or not isinstance(ev, dict):
                return
            self.mark("first_event")
            if ev.get("parent_tool_use_id"):
                return                                    # a helper's own steps are not this turn's own work
            content = (ev.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                return
            kind = ev.get("type")
            for c in content:
                if not isinstance(c, dict):
                    continue
                t = c.get("type")
                if kind == "assistant" and t == "text" and (c.get("text") or "").strip():
                    self.mark("first_text")
                elif kind == "assistant" and t == "tool_use":
                    if c.get("name") in ("Task", "Agent"):
                        self.helper = True
                    tid = c.get("id")
                    if tid:
                        with self._lock:
                            self._tools.setdefault(tid, [self.clock(), None])
                elif kind == "user" and t == "tool_result":
                    tid = c.get("tool_use_id")
                    with self._lock:
                        if tid in self._tools and self._tools[tid][1] is None:
                            self._tools[tid][1] = self.clock()
            if kind == "result":
                self.mark("result")
        except Exception:
            pass

    def _tool_total(self, now):
        """Union of tool intervals (parallel tools are not double counted); open ones run to 'result'/now."""
        spans = sorted((s, e if e is not None else now) for s, e in self._tools.values())
        total, cur_s, cur_e = 0.0, None, None
        for s, e in spans:
            if cur_e is None or s > cur_e:
                if cur_e is not None:
                    total += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        if cur_e is not None:
            total += cur_e - cur_s
        return total

    def finish(self):
        """Result dict. Missing stages are None. Safe to call more than once."""
        try:
            with self._lock:
                m = dict(self._marks)
                now = self.clock()
                tool_s = self._tool_total(m.get("result", now)) if self._tools else None
            m_start, m_sent = m.get("start"), m.get("sent")
            return {
                "ts": int(m_start) if m_start is not None else None,
                "kind": self.kind,
                "model": self.model,
                "helper_turn": bool(self.helper),
                "stt_ms": _ms(m.get("stt_start"), m.get("stt_end")),
                "pre_send_ms": _ms(m_start, m_sent),
                "first_event_ms": _ms(m_sent, m.get("first_event")),
                "first_text_ms": _ms(m_sent, m.get("first_text")),
                "tool_ms_total": None if tool_s is None else int(round(tool_s * 1000)),
                "claude_total_ms": _ms(m_sent, m.get("result")),
                "tts_first_audio_ms": _ms(m.get("reply_ready"), m.get("tts_first_audio")),
                "turn_total_ms": _ms(m_start, m.get("audio_end") or m.get("reply_ready") or m.get("result")),
            }
        except Exception:
            return {}

    def write(self, path):
        """Append one JSON line. Once only per timer. Never raises."""
        try:
            with self._lock:
                if self._written:
                    return False
                self._written = True
            row = self.finish()
            if not row:
                return False
            p = pathlib.Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------- summary
def percentile(values, p):
    vals = sorted(values)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] if lo == hi else vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def read_rows(path, last=50):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if isinstance(r, dict):
                        rows.append(r)
                except ValueError:
                    pass
    except OSError:
        return []
    return rows[-last:] if last else rows


def summarise(rows):
    """{stage: (n, median, p90)} for each stage that has data."""
    out = {}
    for s in ("pre_send_ms",) + STAGES:
        vals = [r[s] for r in rows if isinstance(r.get(s), (int, float))]
        if vals:
            out[s] = (len(vals), percentile(vals, 50), percentile(vals, 90))
    return out


def format_summary(rows):
    if not rows:
        return "No timing data yet."
    lines = [f"Last {len(rows)} turns ({sum(r.get('kind') == 'voice' for r in rows)} voice, "
             f"{sum(bool(r.get('helper_turn')) for r in rows)} with helpers)",
             f"{'stage':<20}{'n':>4}{'median s':>10}{'p90 s':>9}"]
    for s, (n, med, p90) in summarise(rows).items():
        lines.append(f"{s:<20}{n:>4}{med / 1000:>10.1f}{p90 / 1000:>9.1f}")
    return "\n".join(lines)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 50
    print(format_summary(read_rows(pathlib.Path(__file__).resolve().parent / "logs" / "timing.jsonl", n)))
