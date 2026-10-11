"""Tests for timing_log.py: TurnTimer maths, JSON line format (no text), summary stats, effort setting."""
import json
import pathlib
import tempfile
import unittest

import timing_log as T


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def adv(self, s):
        self.t += s


def asst_text(text="SECRET WORDS", parent=None):
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if parent:
        ev["parent_tool_use_id"] = parent
    return ev


def tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": {"command": "SECRET"}}]}}


def tool_result(tid):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "SECRET OUT"}]}}


class TestTurnTimer(unittest.TestCase):
    def full_turn(self):
        c = FakeClock()
        t = T.TurnTimer("voice", "sonnet", clock=c)
        t.mark("stt_start"); c.adv(0.8); t.mark("stt_end")          # stt 800
        c.adv(0.2); t.mark("sent")                                   # pre-send 1000
        c.adv(1.5); t.event({"type": "system"})                      # first event 1500
        c.adv(1.0); t.event(tool_use("a")); c.adv(2.0); t.event(tool_result("a"))   # tool 2000
        c.adv(0.5); t.event(asst_text())                             # first text 5000
        c.adv(0.5); t.event({"type": "result"})                      # claude total 5500
        c.adv(0.1); t.mark("reply_ready"); c.adv(0.3); t.mark("tts_first_audio")    # tts 300
        c.adv(4.0); t.mark("audio_end")                              # total 1+5.5+0.1+0.3+4 = ... see below
        return t

    def test_stage_maths(self):
        # TEST: every stage equals the injected-clock difference in ms
        r = self.full_turn().finish()
        self.assertEqual(r["stt_ms"], 800)
        self.assertEqual(r["pre_send_ms"], 1000)
        self.assertEqual(r["first_event_ms"], 1500)
        self.assertEqual(r["first_text_ms"], 5000)
        self.assertEqual(r["tool_ms_total"], 2000)
        self.assertEqual(r["claude_total_ms"], 5500)
        self.assertEqual(r["tts_first_audio_ms"], 300)
        self.assertEqual(r["turn_total_ms"], 10900)   # 1.0 pre-send + 5.5 claude + 0.1 + 0.3 + 4.0 audio
        self.assertEqual(r["kind"], "voice")
        self.assertEqual(r["model"], "sonnet")
        self.assertFalse(r["helper_turn"])

    def test_missing_marks_are_none(self):
        # TEST: a typed turn with nothing marked has None stages, no exception
        t = T.TurnTimer("typed", clock=FakeClock())
        r = t.finish()
        self.assertIsNone(r["stt_ms"]); self.assertIsNone(r["first_text_ms"]); self.assertIsNone(r["tool_ms_total"])
        self.assertEqual(r["kind"], "typed")

    def test_marks_are_first_wins(self):
        # TEST: marking twice keeps the first time
        c = FakeClock(); t = T.TurnTimer(clock=c)
        t.mark("sent"); c.adv(5); t.mark("sent"); t.mark("first_event")
        self.assertEqual(t.finish()["first_event_ms"], 5000)

    def test_events_before_sent_ignored(self):
        # TEST: late events from an earlier turn (before 'sent') don't set first_event/first_text
        c = FakeClock(); t = T.TurnTimer(clock=c)
        t.event(asst_text()); c.adv(1); t.mark("sent")
        self.assertIsNone(t.finish()["first_text_ms"])

    def test_helper_flag_and_helper_steps_ignored(self):
        # TEST: an Agent tool call flags the turn; a helper's own text (parent set) is not the turn's first text
        c = FakeClock(); t = T.TurnTimer(clock=c); t.mark("sent"); c.adv(1)
        t.event(asst_text(parent="h1"))
        self.assertIsNone(t.finish()["first_text_ms"])
        t.event(tool_use("h1", "Agent"))
        self.assertTrue(t.finish()["helper_turn"])

    def test_parallel_tools_not_double_counted(self):
        # TEST: two overlapping tools count their union (3s), not the sum (4s)
        c = FakeClock(); t = T.TurnTimer(clock=c); t.mark("sent")
        t.event(tool_use("a")); c.adv(1); t.event(tool_use("b")); c.adv(1); t.event(tool_result("a"))
        c.adv(1); t.event(tool_result("b"))
        self.assertEqual(t.finish()["tool_ms_total"], 3000)

    def test_unfinished_tool_runs_to_result(self):
        # TEST: a tool with no result (background helper) is cut off at the result event
        c = FakeClock(); t = T.TurnTimer(clock=c); t.mark("sent")
        t.event(tool_use("a", "Agent")); c.adv(2); t.event({"type": "result"}); c.adv(10)
        self.assertEqual(t.finish()["tool_ms_total"], 2000)

    def test_garbage_events_never_raise(self):
        # TEST: odd input shapes are swallowed
        t = T.TurnTimer(clock=FakeClock()); t.mark("sent")
        for ev in (None, 5, {}, {"message": None}, {"type": "assistant", "message": {"content": "x"}},
                   {"type": "assistant", "message": {"content": [None, 3]}}):
            t.event(ev)
        self.assertIn("kind", t.finish())

    def test_turn_total_falls_back(self):
        # TEST: with no audio (typed, text-only) the total runs to reply_ready, else result
        c = FakeClock(); t = T.TurnTimer(clock=c); t.mark("sent"); c.adv(2); t.mark("result")
        self.assertEqual(t.finish()["turn_total_ms"], 2000)

    def test_write_json_line_no_text(self):
        # TEST: one JSON line, only numbers/flags/short labels; none of the text in events appears
        t = self.full_turn()
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "sub" / "timing.jsonl"       # folder created on demand
            self.assertTrue(t.write(p))
            self.assertFalse(t.write(p))                       # only once per turn
            raw = p.read_text(encoding="utf-8")
        self.assertEqual(len(raw.strip().splitlines()), 1)
        self.assertNotIn("SECRET", raw)
        row = json.loads(raw)
        for k, v in row.items():
            self.assertTrue(v is None or isinstance(v, (int, bool)) or (k in ("kind", "model") and len(v) <= 40), k)

    def test_write_never_raises(self):
        # TEST: an unwritable path returns False
        t = T.TurnTimer(clock=FakeClock())
        self.assertFalse(t.write("Z:\\no\\such\\drive\\\x00bad\\timing.jsonl"))


class TestSummary(unittest.TestCase):
    def test_percentiles(self):
        # TEST: median and p90 with interpolation
        self.assertEqual(T.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(T.percentile([1, 2, 3, 4, 5], 90), 4.6)
        self.assertIsNone(T.percentile([], 50))
        self.assertEqual(T.percentile([7], 90), 7)

    def test_summarise_skips_none_and_formats(self):
        # TEST: None stages are skipped; empty input prints a friendly line
        rows = [{"kind": "voice", "stt_ms": 1000, "claude_total_ms": 4000, "tool_ms_total": None},
                {"kind": "typed", "stt_ms": None, "claude_total_ms": 8000, "helper_turn": True}]
        s = T.summarise(rows)
        self.assertEqual(s["claude_total_ms"], (2, 6000, 7600))
        self.assertNotIn("tool_ms_total", s)
        out = T.format_summary(rows)
        self.assertIn("claude_total_ms", out); self.assertIn("1 voice", out); self.assertIn("1 with helpers", out)
        self.assertEqual(T.format_summary([]), "No timing data yet.")

    def test_read_rows_last_n_and_bad_lines(self):
        # TEST: bad lines skipped, only the last N returned, missing file = []
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "t.jsonl"
            p.write_text('{"a":1}\nnot json\n{"a":2}\n{"a":3}\n', encoding="utf-8")
            self.assertEqual([r["a"] for r in T.read_rows(p, 2)], [2, 3])
            self.assertEqual(T.read_rows(pathlib.Path(d) / "missing.jsonl"), [])


class TestEffort(unittest.TestCase):
    def test_default_and_allowed(self):
        # TEST: default low; every allowed value passes through, case/space insensitive
        self.assertEqual(T.resolve_effort(), "low")
        for v in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(T.resolve_effort(v), v)
        self.assertEqual(T.resolve_effort(" HIGH "), "high")

    def test_empty_means_claude_default(self):
        # TEST: "" / None (empty yaml key) = don't pass the flag
        self.assertEqual(T.resolve_effort(""), "")
        self.assertEqual(T.resolve_effort(None), "")
        self.assertEqual(T.effort_args(None), [])

    def test_invalid_ignored(self):
        # TEST: unknown values and wrong types never reach the command line
        for v in ("turbo", "fast", 5, ["low"], "low;rm"):
            self.assertEqual(T.resolve_effort(v), "", v)

    def test_deep_always_high(self):
        # TEST: 'think hard' forces high whatever the config says, even empty or invalid
        for v in ("low", "", None, "max", "junk"):
            self.assertEqual(T.resolve_effort(v, deep=True), "high")

    def test_command_args(self):
        # TEST: the pieces appended to the claude command
        self.assertEqual(T.effort_args("low"), ["--effort", "low"])
        self.assertEqual(T.effort_args("low", deep=True), ["--effort", "high"])


if __name__ == "__main__":
    unittest.main()
