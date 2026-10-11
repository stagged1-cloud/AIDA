"""Tests for fast_turns.py (simple-vs-complex classifier and set_model helpers). Run: python test_fast_turns.py"""
import json
import unittest

import fast_turns as ft

SIMPLE = [
    "open chrome",                                   # T01 bare open
    "Open VS Code.",                                 # T02 punctuation/case
    "um, close notepad",                             # T03 filler before the order
    "stop the recording",                            # T04 stop
    "record screen one",                             # T05 record
    "take a screenshot",                             # T06 screenshot
    "status",                                        # T07 one word
    "OK, where we at?",                              # T08 his check-in
    "How's it going, mate?",                         # T09 chit-chat
    "cheers",                                        # T10 thanks
    "yeah",                                          # T11 filler only
    "Hey AIDA, play some music",                     # T12 wake phrase + play
    "what's the time?",                              # T13 time
    "mute",                                          # T14 mute
    "go to bbc news",                                # T15 go to
    "Are you on line, AIDA?",                        # T16 transcription quirk
]

COMPLEX = [
    "think hard about the wake word design",         # C01 think hard override
    "open chrome and then search for ADA on GitHub", # C02 multi-step + search
    "record screen one for 30 seconds then play it", # C03 multi-step
    "fix the bug in wake_word.py",                   # C04 fix/bug
    "run the tests",                                 # C05 test
    "check my right screen",                         # C06 screen read = more work
    "reply to Liz's email",                          # C07 email
    "what's happening with the Zendesk ticket",      # C08 work
    "spin up some helpers to research this",         # C09 helpers
    "open the work logs",                            # C10 work + logs (open does not save it)
    "why did you do that?",                          # C11 why = explain
    "build the share copy",                          # C12 build
    "delete the scratch folder",                     # C13 delete
    "confirmed",                                     # C14 guard word never fast
    "restart yourself",                              # C15 restart
    "summarise today",                               # C16 summar*
    "what's the capital of France",                  # C17 unknown question = unsure = complex
    "I want you to record screen one with sound and open it on the right screen not full screen",  # C18 long
    "",                                              # C19 empty
    "   ",                                           # C20 whitespace
    None,                                            # C21 None
    "open chrome, open notepad, open teams, close vs code",  # C22 run-on with commas
    "stop? start? what?",                            # C23 stacked questions
    "testing testing",                               # C24 word-start match on test
    "open the config",                               # C25 config
    "close it, also mute",                           # C26 'also' = multi-step
]


class Classify(unittest.TestCase):
    def test_simple_table(self):
        """Every SIMPLE row is classified simple."""
        for s in SIMPLE:
            with self.subTest(s=s):
                self.assertTrue(ft.is_simple(s), s)

    def test_complex_table(self):
        """Every COMPLEX row (incl. empty/None/long) is classified complex."""
        for s in COMPLEX:
            with self.subTest(s=s):
                self.assertFalse(ft.is_simple(s), repr(s))

    def test_header_ignored(self):
        """The app's [time | humour] header does not count as words."""
        self.assertTrue(ft.is_simple("[Sat 10 Oct 2026 23:30 UK | humour 85% | honesty 95%]\nopen chrome"))

    def test_word_limit(self):
        """Over max_words = complex even if it starts simple."""
        self.assertFalse(ft.is_simple("open " + "chrome " * 20))
        self.assertTrue(ft.is_simple("open chrome", max_words=2))
        self.assertFalse(ft.is_simple("open chrome now please", max_words=2))


class Target(unittest.TestCase):
    def test_simple_goes_fast(self):
        self.assertEqual(ft.target_model("open chrome", "sonnet"), ft.FAST_MODEL_DEFAULT)

    def test_complex_stays_main(self):
        self.assertEqual(ft.target_model("fix the tests", "sonnet"), "sonnet")

    def test_disabled(self):
        """Feature off: always main."""
        self.assertEqual(ft.target_model("open chrome", "sonnet", enabled=False), "sonnet")

    def test_deep_and_mcp_modes(self):
        """'think hard' / connector one-offs never use the fast model."""
        for m in ("deep", "mcp", "deepmcp"):
            self.assertEqual(ft.target_model("open chrome", "sonnet", mode=m), "sonnet")

    def test_no_fast_model(self):
        self.assertEqual(ft.target_model("open chrome", "sonnet", fast_model=""), "sonnet")


class Control(unittest.TestCase):
    def test_request_shape(self):
        line = ft.set_model_request("claude-haiku-5-5", "m1")
        self.assertTrue(line.endswith("\n"))
        ev = json.loads(line)
        self.assertEqual(ev, {"type": "control_request", "request_id": "m1",
                              "request": {"subtype": "set_model", "model": "claude-haiku-5-5"}})

    def test_success(self):
        """Matches the response seen in the live probe."""
        ev = {"type": "control_response", "response": {"subtype": "success", "request_id": "m1"}}
        self.assertTrue(ft.control_ok(ev, "m1"))

    def test_error(self):
        ev = {"type": "control_response", "response": {"subtype": "error", "request_id": "m1", "error": "x"}}
        self.assertFalse(ft.control_ok(ev, "m1"))

    def test_unrelated(self):
        """Other events or other ids are not the answer."""
        self.assertIsNone(ft.control_ok({"type": "assistant"}, "m1"))
        self.assertIsNone(ft.control_ok({"type": "control_response", "response": {"subtype": "success",
                                                                                  "request_id": "m2"}}, "m1"))
        self.assertIsNone(ft.control_ok(None, "m1"))
        self.assertIsNone(ft.control_ok("junk", "m1"))


class Switch(unittest.TestCase):
    """run_switch fallback: anything but a timely success = False (app stays on its current model)."""

    def _q(self, *evs):
        import queue
        q = queue.Queue()
        for e in evs:
            q.put(e)
        return q

    def test_success_and_others_passed_on(self):
        sent, other = [], []
        q = self._q({"type": "assistant"}, {"type": "control_response",
                                            "response": {"subtype": "success", "request_id": "m1"}})
        self.assertTrue(ft.run_switch(sent.append, q, "claude-haiku-5-5", "m1", 2, other.append))
        self.assertEqual(json.loads(sent[0])["request"]["model"], "claude-haiku-5-5")
        self.assertEqual(other, [{"type": "assistant"}])

    def test_error_reply(self):
        q = self._q({"type": "control_response", "response": {"subtype": "error", "request_id": "m1"}})
        self.assertFalse(ft.run_switch(lambda l: None, q, "x", "m1", 2))

    def test_stream_ended(self):
        self.assertFalse(ft.run_switch(lambda l: None, self._q(None), "x", "m1", 2))

    def test_timeout(self):
        """No reply at all: gives up after timeout_s."""
        import time
        t = time.time()
        self.assertFalse(ft.run_switch(lambda l: None, self._q(), "x", "m1", 0.5))
        self.assertLess(time.time() - t, 3)

    def test_write_fails(self):
        """Broken pipe on write: False, no exception."""
        def boom(line):
            raise OSError("pipe closed")
        self.assertFalse(ft.run_switch(boom, self._q(), "x", "m1", 1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
