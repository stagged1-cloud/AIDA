#!/usr/bin/env python
"""Helper ledger: one small JSON file per helper job in state/helpers/.

Usage:
  python helper_ledger.py start "<title>"      -> prints id
  python helper_ledger.py note <id> "<text>"
  python helper_ledger.py done <id> "<summary>"
  python helper_ledger.py fail <id> "<reason>"
  python helper_ledger.py list [--all]
  python helper_ledger.py stale [minutes]      (default 10; marks silent running jobs 'stale')
  python helper_ledger.py lost                 (marks every running job 'lost'; used on startup)

Stdlib only. Writes are atomic (temp file + os.replace).
"""
import os
import sys
import json
import time
import uuid
import pathlib
import datetime

LEDGER_DIR = pathlib.Path(os.environ.get("HELPER_LEDGER_DIR") or
                          pathlib.Path(__file__).resolve().parent / "state" / "helpers")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _path(job_id, d=None):
    return pathlib.Path(d or LEDGER_DIR) / f"{job_id}.json"


def _write(job, d=None):
    p = _path(job["id"], d)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    for attempt in range(10):  # Windows can briefly lock the target
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
    os.replace(tmp, p)


def _read(job_id, d=None):
    """Return job dict, or None if missing/corrupt."""
    for attempt in range(8):  # Windows: file is briefly unreadable while being replaced
        try:
            job = json.loads(_path(job_id, d).read_text(encoding="utf-8"))
            return job if isinstance(job, dict) and "id" in job else None
        except PermissionError:
            time.sleep(0.01 * (attempt + 1))
        except (OSError, ValueError):
            return None
    return None


def all_jobs(d=None):
    """All readable jobs, oldest first. Corrupt files are skipped."""
    out = []
    base = pathlib.Path(d or LEDGER_DIR)
    if not base.exists():
        return out
    for f in base.glob("*.json"):
        job = _read(f.stem, base)
        if job:
            out.append(job)
    return sorted(out, key=lambda j: j.get("started_at", ""))


def start(title, d=None):
    job_id = datetime.datetime.now().strftime("%H%M%S") + "-" + uuid.uuid4().hex[:4]
    now = _now()
    _write({"id": job_id, "title": title, "started_at": now, "last_update": now,
            "status": "running", "latest_note": "", "final_summary": ""}, d)
    return job_id


def _update(job_id, d=None, **fields):
    job = _read(job_id, d)
    if job is None:
        raise KeyError(f"no such job (or unreadable): {job_id}")
    job.update(fields)
    job["last_update"] = _now()
    _write(job, d)
    return job


def note(job_id, text, d=None):
    return _update(job_id, d, latest_note=text)


def done(job_id, summary, d=None):
    return _update(job_id, d, status="done", final_summary=summary)


def fail(job_id, reason, d=None):
    return _update(job_id, d, status="failed", final_summary=reason)


def mark_stale(minutes=10, d=None):
    """Mark 'running' jobs with no update for `minutes` as 'stale'. Returns list of jobs changed."""
    changed = []
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
    for job in all_jobs(d):
        if job.get("status") != "running":
            continue
        try:
            last = datetime.datetime.fromisoformat(job.get("last_update", ""))
        except ValueError:
            last = datetime.datetime.min
        if last < cutoff:
            job["status"] = "stale"
            _write(job, d)  # keep last_update as the time of the final real note
            changed.append(job)
    return changed


def mark_lost(d=None):
    """Mark every 'running' (or 'stale') job as 'lost' (assistant restarted). Returns list changed."""
    changed = []
    for job in all_jobs(d):
        if job.get("status") in ("running", "stale"):
            job["status"] = "lost"
            job["final_summary"] = job.get("final_summary") or "Assistant restarted while this was running."
            _write(job, d)
            changed.append(job)
    return changed


def _fmt(job):
    return (f"{job['id']}  [{job.get('status')}]  {job.get('title')}  "
            f"(started {job.get('started_at')}, updated {job.get('last_update')})"
            + (f"\n    note: {job['latest_note']}" if job.get("latest_note") else "")
            + (f"\n    result: {job['final_summary']}" if job.get("final_summary") else ""))


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "start" and args:
            print(start(" ".join(args)))
        elif cmd == "note" and len(args) >= 2:
            note(args[0], " ".join(args[1:]))
        elif cmd == "done" and len(args) >= 2:
            done(args[0], " ".join(args[1:]))
        elif cmd == "fail" and len(args) >= 2:
            fail(args[0], " ".join(args[1:]))
        elif cmd == "list":
            mark_stale(10)
            jobs = all_jobs()
            if "--all" not in args:
                jobs = jobs[-20:]
            print("\n".join(_fmt(j) for j in jobs) or "No helper jobs recorded.")
        elif cmd == "stale":
            changed = mark_stale(int(args[0]) if args else 10)
            print("\n".join(_fmt(j) for j in changed) or "None stale.")
        elif cmd == "lost":
            changed = mark_lost()
            print("\n".join(_fmt(j) for j in changed) or "None lost.")
        else:
            print(__doc__)
            return 1
    except KeyError as e:
        print(f"error: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
