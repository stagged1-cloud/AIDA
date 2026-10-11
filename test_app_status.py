"""Tests for app_status.py (the heartbeat that lets restart_app.ps1 wait until AIDA has finished talking)."""
import json
import tempfile
import unittest
from pathlib import Path

import app_status


class FakeMouth:
    def __init__(self, speaking): self.speaking = speaking


class FakeApp:
    def __init__(self, working=False, speaking=False):
        self.working, self.mouth = working, FakeMouth(speaking)


class TestSnapshot(unittest.TestCase):
    def test_idle(self):
        s = app_status.snapshot(FakeApp(False, False), clock=lambda: 100.0)
        self.assertEqual(s, {"t": 100.0, "working": False, "speaking": False})

    def test_working_and_speaking_flags(self):
        s = app_status.snapshot(FakeApp(True, True))
        self.assertTrue(s["working"] and s["speaking"])

    def test_broken_app_counts_as_idle_and_never_raises(self):
        class Broken:
            @property
            def working(self): raise RuntimeError("boom")
        s = app_status.snapshot(Broken())               # no mouth attribute either
        self.assertFalse(s["working"]); self.assertFalse(s["speaking"])


class TestWrite(unittest.TestCase):
    def test_atomic_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "status.json"
            app_status.write_atomic({"t": 1.5, "working": True, "speaking": False}, p)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["working"], True)
            self.assertFalse(p.with_suffix(".tmp").exists())   # temp file replaced, not left behind


class TestRestartScript(unittest.TestCase):
    """restart_app.ps1 must wait for idleness, not for a fixed time (static checks on the script text)."""
    SRC = (Path(__file__).parent / "restart_app.ps1").read_text(encoding="utf-8")

    def test_reads_the_status_file(self):
        self.assertIn("app_status.json", self.SRC)
        self.assertIn("speaking", self.SRC)

    def test_has_a_max_wait_and_stale_guard(self):
        self.assertIn("MaxWait", self.SRC)
        self.assertIn("-gt 10", self.SRC)                        # stale heartbeat does not block the restart


if __name__ == "__main__":
    unittest.main()
