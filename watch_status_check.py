"""Print state\\watch_status.json plainly and flag problems. Usage: python watch_status_check.py [path]
Exit code 0 = no problems, 1 = problems flagged, 2 = no status file."""
import json
import sys
import time
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parent / "state" / "watch_status.json"
MAX_AGE_S = 30


def problems(d, now=None):
    now = time.time() if now is None else now
    out = []
    age = now - float(d.get("t", 0))
    if age > MAX_AGE_S:
        out.append(f"status file is {int(age)} s old: writer thread stopped or the program is not running")
    w = d.get("watch") or {}
    if w.get("error"):
        out.append(f"watcher status failed: {w['error']}")
    elif w.get("present"):
        if not w.get("running"):
            out.append("screen watch thread is not running")
        for k, m in (w.get("monitors") or {}).items():
            a = m.get("newest_frame_age_s")
            if a is not None and a > MAX_AGE_S:
                out.append(f"monitor {k} ({m.get('label')}) newest frame is {int(a)} s old")
            elif a is None and w.get("watching"):
                out.append(f"monitor {k} ({m.get('label')}) has no frame in the ring")
            if m.get("errors_since_last"):
                out.append(f"monitor {k} ({m.get('label')}) errors rising: +{m['errors_since_last']} "
                           f"({m.get('last_error') or 'unknown'})")
    else:
        out.append("screen watch is not set up")
    t = d.get("screen_text") or {}
    if t.get("present"):
        if t.get("error"):
            out.append(f"screen text status failed: {t['error']}")
        elif not t.get("running"):
            out.append("screen text thread is not running")
        elif t.get("last_pass_age_s") is not None and t["last_pass_age_s"] > MAX_AGE_S:
            out.append(f"screen text last pass {int(t['last_pass_age_s'])} s ago")
    c = d.get("commentary") or {}
    if c.get("present"):
        if c.get("error"):
            out.append(f"commentary status failed: {c['error']}")
        elif c.get("enabled") and not c.get("running"):
            out.append("commentary is on but its thread is not running")
        elif c.get("enabled") and c.get("last_look_age_s") is not None and c["last_look_age_s"] > 120:
            out.append(f"commentary last look {int(c['last_look_age_s'])} s ago")
    return out


def render(d, now=None):
    now = time.time() if now is None else now
    lines = [f"Status written {d.get('written', '?')} ({int(now - float(d.get('t', 0)))} s ago)"]
    w = d.get("watch") or {}
    if w.get("present") and not w.get("error"):
        lines.append(f"Screen watch: {w.get('state')}, grabs {w.get('grabs')}, changes {w.get('events')}, "
                     f"moving keeps {w.get('moving_keeps')}, errors {w.get('errors')}, "
                     f"ring {w.get('ring_frames')} frames / {w.get('ring_bytes', 0) // 1024} KB")
        for k, m in (w.get("monitors") or {}).items():
            lines.append(f"  monitor {k} ({m.get('label')}, rect {m.get('rect')}): via {m.get('backend')}, "
                         f"grabs {m.get('grabs')}, errors {m.get('errors')}"
                         + (f" (last {m['last_error']})" if m.get("last_error") else "")
                         + f", newest frame {m.get('newest_frame_age_s')} s old, {m.get('ring_frames')} frames")
    t = d.get("screen_text") or {}
    if t.get("present") and not t.get("error"):
        lines.append(f"Screen text: {'running' if t.get('running') else 'stopped'}, passes {t.get('passes')}, "
                     f"failures {t.get('failures')}, hints {t.get('hints')}, OCR {t.get('ocr')}, "
                     f"last pass {t.get('last_pass_age_s')} s ago")
    c = d.get("commentary") or {}
    if c.get("present") and not c.get("error"):
        lines.append(f"Commentary: {'on' if c.get('enabled') else 'off'}, looks {c.get('looks')}, "
                     f"silent {c.get('silent')}, deaths {c.get('deaths')}, busy {c.get('busy')}, cap {c.get('cap')}, "
                     f"errors {c.get('errors')}, last decision {c.get('last_decision') or '-'}, "
                     f"last look {c.get('last_look_age_s')} s ago")
    p = problems(d, now)
    lines.append("Problems: none" if not p else "PROBLEMS:\n  " + "\n  ".join(p))
    return "\n".join(lines), p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else STATUS_PATH
    if not path.exists():
        print(f"No status file at {path}. The program has not written one (not restarted since this was added?).")
        return 2
    text, p = render(json.loads(path.read_text(encoding="utf-8")))
    print(text)
    return 1 if p else 0


if __name__ == "__main__":
    sys.exit(main())
