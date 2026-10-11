"""Tests for speech_hold.py and its wiring into Mouth.say / App._user_talking (voice_claude.py).

Bug (11 Oct 2026 00:00): a reply or helper result spoken while he was mid-speech tripped the echo gate,
which blanked his push-to-talk audio (or aborted the open-mic capture), so his words were lost.
Fix: results that arrive while he is talking are held, then released in order once he has finished.
Run: .venv\\Scripts\\python.exe -m unittest test_speech_hold -v
"""
import threading
import time
import unittest

import speech_hold
import test_mute                                   # reuses its AST-lifted real Mouth (no audio, no models)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_hold(talking, clock=None, max_hold_s=180.0):
    out = []
    h = speech_hold.SpeechHold(out.append, lambda: talking[0], max_hold_s=max_hold_s,
                               clock=clock or FakeClock())
    return h, out


class TestSpeechHold(unittest.TestCase):
    # T1: not talking -> the line goes straight through, nothing held
    def test_passes_through_when_quiet(self):
        talking = [False]
        h, out = make_hold(talking)
        self.assertFalse(h.offer("hello", start_thread=False))
        self.assertEqual(out, ["hello"])
        self.assertEqual(h.held, [])

    # T2: a result arriving mid-speech is held, not spoken
    def test_held_mid_speech(self):
        talking = [True]
        h, out = make_hold(talking)
        self.assertTrue(h.offer("helper finished", start_thread=False))
        self.assertEqual(out, [], "nothing may reach the speech queue while he talks")
        self.assertEqual(h.release(), 0, "still talking: stays held")
        self.assertEqual(out, [])

    # T3: released once he finishes, all lines, original order
    def test_released_in_order_after_he_finishes(self):
        talking = [True]
        h, out = make_hold(talking)
        for t in ("one", "two", "three"):
            h.offer(t, start_thread=False)
        talking[0] = False
        self.assertEqual(h.release(), 3)
        self.assertEqual(out, ["one", "two", "three"])
        self.assertEqual(h.held, [])

    # T4: ordering kept even if he stops talking before the watcher fires: a new line queues behind the held ones
    def test_new_line_waits_behind_held(self):
        talking = [True]
        h, out = make_hold(talking)
        h.offer("first", start_thread=False)
        talking[0] = False
        self.assertTrue(h.offer("second", start_thread=False), "must not jump the queue")
        self.assertEqual(out, [])
        h.release()
        self.assertEqual(out, ["first", "second"])

    # T5: safety net: a stuck 'talking' flag (talk key left open) still releases after max_hold_s; nothing lost
    def test_timeout_releases(self):
        talking, clock = [True], FakeClock()
        h, out = make_hold(talking, clock, max_hold_s=60)
        h.offer("late result", start_thread=False)
        clock.t = 59
        self.assertEqual(h.release(), 0)
        clock.t = 60
        self.assertEqual(h.release(), 1)
        self.assertEqual(out, ["late result"])

    # T6: a broken talking-check never swallows speech (treated as not talking)
    def test_broken_check_passes_through(self):
        out = []
        h = speech_hold.SpeechHold(out.append, lambda: 1 / 0)
        self.assertFalse(h.offer("x", start_thread=False))
        self.assertEqual(out, ["x"])

    # T7: the real background watcher releases on its own once he stops (thread path, short poll)
    def test_watcher_thread_releases(self):
        talking, out = [True], []
        h = speech_hold.SpeechHold(out.append, lambda: talking[0], poll_s=0.02)
        h.offer("a")
        h.offer("b")
        time.sleep(0.1)
        self.assertEqual(out, [])
        talking[0] = False
        for _ in range(100):
            if len(out) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(out, ["a", "b"])
        time.sleep(0.1)
        self.assertIsNone(h._thread, "watcher exits when nothing is held")
        talking[0] = True
        h.offer("c")                       # a second hold must start a fresh watcher
        talking[0] = False
        for _ in range(100):
            if len(out) == 3:
                break
            time.sleep(0.02)
        self.assertEqual(out, ["a", "b", "c"])


class TestMouthWiring(unittest.TestCase):
    """The real Mouth.say (lifted from voice_claude.py) with a SpeechHold attached."""

    # T8: while he talks the line is NOT in the mouth queue, so mouth.speaking stays False and the echo gate
    # stays open: his audio is preserved, not blanked
    def test_mouth_say_held_keeps_mic_open(self):
        m = test_mute.make_mouth()
        talking = [True]
        m.hold = speech_hold.SpeechHold(m.q.put, lambda: talking[0])
        m.hold.offer = lambda text, f=m.hold.offer: f(text, start_thread=False)
        m.say("The settings file is waiting on your confirmed.")
        self.assertTrue(m.q.empty())
        self.assertFalse(m.speaking, "echo gate must not block while the reply is only held")
        talking[0] = False
        m.hold.release()
        self.assertEqual(m.q.get_nowait(), "The settings file is waiting on your confirmed.")

    # T9: markers are still never spoken or held
    def test_marker_not_held(self):
        m = test_mute.make_mouth()
        m.hold = speech_hold.SpeechHold(m.q.put, lambda: True)
        m.say("[quiet]")
        self.assertEqual(m.hold.held, [])
        self.assertTrue(m.q.empty())


class TestDonTalking(unittest.TestCase):
    """App._user_talking lifted from voice_claude.py: what counts as 'You is mid-speech'."""

    def make(self, recording, wake):
        cls = test_mute.lift(test_mute.VOICE_TREE, "App", ["_user_talking"])
        a = cls.__new__(cls)
        a.recording = recording
        a.wake_state = lambda: wake
        return a

    # T10: talk key open -> talking; open mic woken/capturing -> talking; idle/followup/off -> not
    def test_states(self):
        self.assertTrue(self.make(True, "off")._user_talking())
        self.assertTrue(self.make(False, "woken")._user_talking())
        self.assertTrue(self.make(False, "capturing")._user_talking())
        for st in ("idle", "followup", "off", "unavailable"):
            self.assertFalse(self.make(False, st)._user_talking(), st)


if __name__ == "__main__":
    unittest.main()
