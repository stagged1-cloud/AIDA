"""Tests for helper_ledger.py. Run: python -m pytest test_helper_ledger.py"""
import json
import datetime
import threading

import helper_ledger as hl


def test_start_note_done(tmp_path):
    # start creates a running job with empty note/summary
    jid = hl.start("Research X", tmp_path)
    job = hl._read(jid, tmp_path)
    assert job["status"] == "running" and job["title"] == "Research X"
    assert job["latest_note"] == "" and job["final_summary"] == ""
    # note updates latest_note only
    hl.note(jid, "halfway", tmp_path)
    assert hl._read(jid, tmp_path)["latest_note"] == "halfway"
    # done sets status and summary
    hl.done(jid, "all good", tmp_path)
    job = hl._read(jid, tmp_path)
    assert job["status"] == "done" and job["final_summary"] == "all good"


def test_fail(tmp_path):
    jid = hl.start("Build", tmp_path)
    hl.fail(jid, "compile error", tmp_path)
    job = hl._read(jid, tmp_path)
    assert job["status"] == "failed" and job["final_summary"] == "compile error"


def test_unknown_id_raises(tmp_path):
    # invalid input: noting a job that does not exist
    try:
        hl.note("nope", "x", tmp_path)
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def _age(jid, d, minutes):
    job = hl._read(jid, d)
    job["last_update"] = (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    hl._write(job, d)


def test_stale_detection(tmp_path):
    old = hl.start("old", tmp_path)
    fresh = hl.start("fresh", tmp_path)
    finished = hl.start("finished", tmp_path)
    hl.done(finished, "ok", tmp_path)
    _age(old, tmp_path, 30)
    _age(finished, tmp_path, 30)  # done jobs must never go stale
    changed = hl.mark_stale(10, tmp_path)
    assert [j["id"] for j in changed] == [old]
    assert hl._read(old, tmp_path)["status"] == "stale"
    assert hl._read(fresh, tmp_path)["status"] == "running"
    assert hl._read(finished, tmp_path)["status"] == "done"


def test_lost_on_restart(tmp_path):
    running = hl.start("a", tmp_path)
    stale = hl.start("b", tmp_path)
    _age(stale, tmp_path, 60)
    hl.mark_stale(10, tmp_path)
    ok = hl.start("c", tmp_path)
    hl.done(ok, "fine", tmp_path)
    lost = hl.mark_lost(tmp_path)
    assert {j["id"] for j in lost} == {running, stale}
    assert hl._read(running, tmp_path)["status"] == "lost"
    assert hl._read(ok, tmp_path)["status"] == "done"
    assert hl.mark_lost(tmp_path) == []  # idempotent


def test_corrupt_json_is_skipped(tmp_path):
    good = hl.start("good", tmp_path)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1,2]", encoding="utf-8")
    assert [j["id"] for j in hl.all_jobs(tmp_path)] == [good]
    hl.mark_stale(1, tmp_path)  # must not raise
    hl.mark_lost(tmp_path)      # must not raise
    assert (tmp_path / "bad.json").read_text() == "{not json"  # left untouched
    try:
        hl.note("bad", "x", tmp_path)
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for corrupt job")


def test_concurrent_writes_atomic(tmp_path):
    jid = hl.start("busy", tmp_path)
    errors = []

    def worker(n):
        try:
            for i in range(25):
                hl.note(jid, f"w{n}-{i}", tmp_path)
        except Exception as e:  # noqa
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                try:
                    txt = hl._path(jid, tmp_path).read_text(encoding="utf-8")
                except PermissionError:  # Windows lock during os.replace is fine
                    continue
                json.loads(txt)  # never half-written
        except Exception as e:  # noqa
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)] + [threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    assert hl._read(jid, tmp_path)["latest_note"].startswith("w")
    assert not list(tmp_path.glob("*.tmp"))  # no temp files left behind


def test_cli_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hl, "LEDGER_DIR", tmp_path)
    assert hl.main(["start", "CLI job"]) == 0
    jid = capsys.readouterr().out.strip()
    assert hl.main(["note", jid, "working"]) == 0
    assert hl.main(["done", jid, "finished"]) == 0
    hl.main(["list"])
    out = capsys.readouterr().out
    assert "CLI job" in out and "[done]" in out and "finished" in out
    assert hl.main(["note", "missing", "x"]) == 2
    assert hl.main(["bogus"]) == 1
