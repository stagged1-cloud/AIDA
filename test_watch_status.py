"""Tests for watch_status.py (status file + save-a-frame) and watch_status_check.py."""
import json
import os
import shutil
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import numpy as np

import screen_watch
import watch_status
import watch_status_check

TITLE = "SECRET-WINDOW-TITLE Notepad"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeBackend:
    def __init__(self):
        self.using = {}
        self.n = 0

    def monitors(self):
        return [1, 2]

    def layout(self):
        return {1: (1920, 0, 1920, 1080), 2: (0, 0, 1920, 1080)}

    def grab(self, monitor):
        self.n += 1
        a = np.zeros((64, 64, 4), dtype=np.uint8)
        a[..., :3] = (self.n * 37 + monitor * 11) % 255
        return a

    def close(self):
        pass


def make_watch(clock):
    w = screen_watch.ScreenWatch({"screen_watch_enabled": True, "screen_watch_settle": 0}, clock=clock,
                                 backend_factory=FakeBackend, base=tempfile.mkdtemp())
    w.tick()
    return w


class FakeText:
    def __init__(self, clock):
        self.counters = Counter(passes=5, hints=1, ocr=2, errors=0)
        self.running = True
        self.last_pass_ts = clock() - 3
        self.title = TITLE                       # must never reach the status file


class FakeComm:
    def __init__(self, clock):
        self.clock = clock
        self.enabled = True
        self.counters = {"looks": 4, "lines": 1, "silent": 3, "deaths": 1, "skipped_busy": 2, "skipped_cap": 0,
                         "errors": 0}
        self.last_reason = "silent"
        self.last_look = clock() - 5
        self._thread = None


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.watch = make_watch(self.clock)
        self.clock.t += 2

    def test_content(self):
        d = watch_status.snapshot(self.watch, FakeText(self.clock), FakeComm(self.clock), clock=self.clock)
        m = d["watch"]["monitors"]
        self.assertEqual(m["2"]["label"], "left")
        self.assertEqual(m["1"]["label"], "right")
        self.assertEqual(m["1"]["grabs"], 1)
        self.assertEqual(m["1"]["ring_frames"], 1)
        self.assertEqual(m["1"]["newest_frame_age_s"], 2.0)
        self.assertEqual(d["screen_text"]["last_pass_age_s"], 3.0)
        self.assertEqual(d["commentary"]["deaths"], 1)
        self.assertEqual(d["commentary"]["last_look_age_s"], 5.0)

    def test_no_image_data_or_titles(self):
        d = watch_status.snapshot(self.watch, FakeText(self.clock), FakeComm(self.clock), clock=self.clock)
        s = json.dumps(d)
        self.assertNotIn("SECRET", s)
        self.assertNotIn("jpeg", s.lower())
        self.assertLess(len(s), 4000)                # numbers only; a frame would be KBs
        for v in json.loads(s)["watch"]["monitors"].values():
            for x in v.values():
                self.assertFalse(isinstance(x, str) and len(x) > 60)

    def test_errors_since_last(self):
        prev = watch_status.snapshot(self.watch, clock=self.clock)
        self.watch.per_monitor[1]["errors"] += 3
        d = watch_status.snapshot(self.watch, clock=self.clock, prev=prev)
        self.assertEqual(d["watch"]["monitors"]["1"]["errors_since_last"], 3)

    def test_broken_part_never_raises(self):
        class Bad:
            counters = None
        d = watch_status.snapshot(None, Bad(), None, clock=self.clock)
        self.assertIn("error", d["screen_text"])

    def test_atomic_write(self):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "state" / "watch_status.json"
        real = os.replace
        with mock.patch("watch_status.os.replace", side_effect=lambda a, b: real(a, b)) as rep:
            watch_status.write_atomic({"t": 1}, p)
            self.assertEqual(Path(rep.call_args[0][0]).suffix, ".tmp")
        self.assertEqual(json.loads(p.read_text()), {"t": 1})
        self.assertFalse(p.with_suffix(".tmp").exists())
        shutil.rmtree(tmp)

    def test_disabled_starts_nothing(self):
        self.assertIsNone(watch_status.start(object(), {"watch_status_enabled": False}))


class CheckerTests(unittest.TestCase):
    def base(self, now):
        return {"t": now, "written": "x",
                "watch": {"present": True, "running": True, "watching": True,
                          "monitors": {"1": {"label": "left", "newest_frame_age_s": 2, "errors_since_last": 0}}},
                "screen_text": {"present": True, "running": True, "last_pass_age_s": 1},
                "commentary": {"present": True, "enabled": False}}

    def test_healthy(self):
        now = time.time()
        self.assertEqual(watch_status_check.problems(self.base(now), now), [])

    def test_flags(self):
        now = time.time()
        d = self.base(now - 60)
        d["watch"]["running"] = False
        d["watch"]["monitors"]["1"].update(newest_frame_age_s=45, errors_since_last=2, last_error="OSError")
        d["screen_text"]["last_pass_age_s"] = 40
        p = " | ".join(watch_status_check.problems(d, now))
        for s in ("status file", "not running", "45 s old", "errors rising", "screen text last pass"):
            self.assertIn(s, p)

    def test_main_exit_codes(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(watch_status_check.main([str(tmp / "none.json")]), 2)
        f = tmp / "s.json"
        f.write_text(json.dumps(self.base(time.time())))
        self.assertEqual(watch_status_check.main([str(f)]), 0)
        shutil.rmtree(tmp)


class SaveCommandTests(unittest.TestCase):
    def test_parse(self):
        for t in ("save a frame", "save the screen", "show me what the watcher sees", "ok save a frame",
                  "save the screens"):
            self.assertTrue(watch_status.parse_save_command(t), t)
        for t in ("save the file", "what does the screen show", "save a frame of the document for me and email it"):
            self.assertFalse(watch_status.parse_save_command(t), t)

    def test_save_path(self):
        clock = Clock()
        w = make_watch(clock)
        clock.t += 4
        tmp = Path(tempfile.mkdtemp()) / "shots"
        import datetime
        saved = watch_status.save_frames(w, tmp, clock=clock, now=datetime.datetime(2026, 10, 11, 16, 30, 5))
        names = sorted(p.name for p, *_ in saved)
        self.assertEqual(names, ["watcher-20261011-163005-mon1.jpg", "watcher-20261011-163005-mon2.jpg"])
        for p, *_ in saved:
            self.assertEqual(p.read_bytes()[:2], b"\xff\xd8")
        reply = watch_status.save_reply(saved)
        self.assertIn("4 seconds old", reply)
        self.assertIn("left", reply)
        shutil.rmtree(tmp.parent)

    def test_nothing_to_save(self):
        clock = Clock()
        w = screen_watch.ScreenWatch({"screen_watch_enabled": True}, clock=clock, backend_factory=FakeBackend,
                                     base=tempfile.mkdtemp())
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(watch_status.save_frames(w, tmp, clock=clock), [])
        self.assertIn("Nothing to save", watch_status.save_reply([]))


if __name__ == "__main__":
    unittest.main()
