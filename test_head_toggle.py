"""Tests for head_toggle.py (head on/off switch). No real windows, no GL: a fake face stands in."""
import threading
import unittest

import head_toggle as ht


class FakeFace:
    def __init__(self):
        self.closed = False


class Rig:
    """Controller wired to fakes; records every call."""
    def __init__(self, fail_make=False, fail_close=False, state=None):
        self.made, self.closed, self.logs, self.changes, self.saved = [], [], [], [], []
        self.fail_make, self.fail_close = fail_make, fail_close
        self.state = state if state is not None else {}
        self.c = ht.HeadController(self._make, self._close, self.state, lambda s: self.saved.append(dict(s)),
                                   log=lambda who, t: self.logs.append((who, t)), on_change=self.changes.append)

    def _make(self):
        if self.fail_make:
            raise RuntimeError("no GPU")
        f = FakeFace()
        self.made.append(f)
        return f

    def _close(self, f):
        if self.fail_close:
            raise RuntimeError("window already gone")
        f.closed = True
        self.closed.append(f)


class StateMachine(unittest.TestCase):
    def test_off_then_on(self):                       # basic cycle
        r = Rig()
        r.c.start(True)
        self.assertTrue(r.c.is_on)
        self.assertFalse(r.c.set(False))
        self.assertTrue(r.closed[0].closed)
        self.assertTrue(r.c.set(True))
        self.assertEqual(len(r.made), 2)               # a fresh face, not the closed one

    def test_start_off_creates_nothing(self):          # config/state says off at start-up
        r = Rig()
        r.c.start(False)
        self.assertFalse(r.c.is_on)
        self.assertEqual(r.made, [])
        self.assertEqual(r.changes, [False])

    def test_idempotent(self):                         # 'head on' when already on must not make a second face
        r = Rig()
        r.c.set(True)
        r.c.set(True)
        self.assertEqual(len(r.made), 1)
        r.c.set(False)
        r.c.set(False)
        self.assertEqual(len(r.closed), 1)

    def test_repeated_toggles(self):                   # hammer the button
        r = Rig()
        for _ in range(101):
            r.c.toggle()
        self.assertTrue(r.c.is_on)                     # odd count from off
        self.assertEqual(len(r.made), len(r.closed) + 1)

    def test_log_lines(self):                          # app log style
        r = Rig()
        r.c.set(True)
        r.c.set(False)
        self.assertEqual(r.logs, [("face", "head on"), ("face", "head off")])

    def test_toggle_while_speaking(self):              # re-entrant call from inside close() is ignored, no crash
        r = Rig()
        r.c.set(True)
        inner = []

        def close_reentrant(f):
            inner.append(r.c.set(True))                # e.g. a queued click lands mid-teardown
            f.closed = True
        r.c.close = close_reentrant
        self.assertFalse(r.c.set(False))
        self.assertEqual(len(r.made), 1)               # inner call did nothing
        self.assertEqual(inner, [False])               # it only reported the current state, without acting

    def test_threads_hammering(self):                  # real threads, lock must keep faces == closes + (0|1)
        r = Rig()
        ts = [threading.Thread(target=lambda: [r.c.toggle() for _ in range(50)]) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(r.made) - len(r.closed), 1 if r.c.is_on else 0)

    def test_schedule_used_for_requests(self):         # request() goes via the UI scheduler, not straight in
        q = []
        r = Rig()
        r.c.schedule = q.append
        r.c.request(True)
        self.assertEqual(r.made, [])
        q.pop()()
        self.assertTrue(r.c.is_on)


class Failures(unittest.TestCase):
    def test_make_fails_stays_off_and_logs(self):      # face creation failure
        r = Rig(fail_make=True)
        self.assertFalse(r.c.set(True))
        self.assertFalse(r.c.is_on)
        self.assertIn("could not start", r.logs[0][1])
        self.assertEqual(r.state.get("face_enabled"), None)   # failed wish is not saved as 'on'

    def test_recovers_after_failure(self):
        r = Rig(fail_make=True)
        r.c.set(True)
        r.fail_make = False
        self.assertTrue(r.c.set(True))

    def test_close_fails_still_off(self):              # shutdown error must not leave a zombie 'on'
        r = Rig(fail_close=True)
        r.c.set(True)
        self.assertFalse(r.c.set(False))
        self.assertFalse(r.c.is_on)

    def test_start_never_raises(self):
        Rig(fail_make=True).c.start(True)


class Persistence(unittest.TestCase):
    def test_write_on_toggle(self):
        r = Rig()
        r.c.set(True)
        r.c.set(False)
        self.assertEqual([s["face_enabled"] for s in r.saved], [True, False])

    def test_state_beats_config(self):                 # voice/button choice wins over config.yaml
        self.assertFalse(ht.initial({"face_enabled": False}, {"face_enabled": True}))
        self.assertTrue(ht.initial({"face_enabled": True}, {"face_enabled": False}))

    def test_config_default(self):                     # nothing remembered -> config; nothing at all -> on
        self.assertFalse(ht.initial({}, {"face_enabled": False}))
        self.assertTrue(ht.initial({}, {}))
        self.assertTrue(ht.initial(None, None))

    def test_garbage_state_ignored(self):              # non-bool in state.json falls back to config
        self.assertFalse(ht.initial({"face_enabled": "yes"}, {"face_enabled": False}))

    def test_start_does_not_overwrite_choice(self):    # start-up from config must not write state.json
        r = Rig()
        r.c.start(True)
        self.assertEqual(r.saved, [])


class Parsing(unittest.TestCase):
    def test_off(self):
        for s in ("head off", "Head off.", "turn the head off", "okay turn the head off", "switch off the head",
                  "hide the head", "turn the animated head off please", "face off"):
            self.assertIs(ht.parse_command(s), False, s)

    def test_on(self):
        for s in ("head on", "Head on!", "turn the head on", "OK, turn the animated head on", "show the head",
                  "switch on the floating head"):
            self.assertIs(ht.parse_command(s), True, s)

    def test_not_commands(self):                       # must not hijack normal sentences
        for s in ("", None, "head", "headache on", "what is on your head", "turn the heating off",
                  "head off to the shop and check the weather", "watcher on", "humour 80"):
            self.assertIsNone(ht.parse_command(s), s)


if __name__ == "__main__":
    unittest.main()
