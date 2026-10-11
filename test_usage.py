"""Tests for usage.py (context-fill bar data) and its wiring. No network, no real CLI, no windows opened.
Run: python -m unittest test_usage
"""
import json
import os
import pathlib
import tempfile
import unittest

import usage as u

HERE = pathlib.Path(__file__).resolve().parent

# Real usage numbers from a local transcript line; ALL text stripped (only ids and counts remain).
REAL_TRANSCRIPT_LINE = json.dumps({
    "type": "assistant", "isSidechain": False, "sessionId": "00000000-0000-0000-0000-000000000000",
    "message": {"model": "claude-sonnet-5-5", "content": [],
                "usage": {"input_tokens": 2, "cache_creation_input_tokens": 502, "cache_read_input_tokens": 215449,
                          "output_tokens": 4278, "output_tokens_details": {"thinking_tokens": 1131},
                          "service_tier": "standard"}}})
REAL_USED = 2 + 502 + 215449 + 4278


def asst(inp=0, cr=0, cc=0, out=0, model="claude-sonnet-5-5", parent=None, sid="s1"):
    return {"type": "assistant", "session_id": sid, "parent_tool_use_id": parent,
            "message": {"model": model, "content": [], "usage": {"input_tokens": inp, "cache_read_input_tokens": cr,
                        "cache_creation_input_tokens": cc, "output_tokens": out}}}


def result(model="claude-sonnet-5-5", window=1_000_000):
    return {"type": "result", "modelUsage": {model: {"contextWindow": window}}}


class Parsing(unittest.TestCase):
    def test_real_transcript_line(self):                      # real sample: summed input+cache+output
        self.assertEqual(u.parse_assistant_line(REAL_TRANSCRIPT_LINE), ("claude-sonnet-5-5", REAL_USED))

    def test_sidechain_and_helper_ignored(self):              # helpers' turns are not the main conversation
        self.assertIsNone(u.parse_assistant_line(dict(json.loads(REAL_TRANSCRIPT_LINE), isSidechain=True)))
        self.assertIsNone(u.parse_assistant_line(asst(inp=5, parent="toolu_1")))

    def test_junk_lines(self):                                # invalid input never raises
        for bad in ("", "not json", "[]", "{}", '{"type":"user"}', '{"type":"assistant"}',
                    '{"type":"assistant","message":{"usage":{}}}', b"\xff"):
            self.assertIsNone(u.parse_assistant_line(bad))

    def test_synthetic_model_ignored(self):
        self.assertIsNone(u.parse_assistant_line(asst(inp=9, model="<synthetic>")))

    def test_tokens_ignore_bad_values(self):
        self.assertEqual(u.tokens_from_usage({"input_tokens": 10, "output_tokens": "x", "cache_read_input_tokens": -5}), 10)
        self.assertIsNone(u.tokens_from_usage(None))
        self.assertIsNone(u.tokens_from_usage({"input_tokens": True}))


class Maths(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(u.context_usage(123_000, "m", 1_000_000)["percent"], 12.3)

    def test_missing_data(self):                              # no figures: n/a, no crash
        s = u.context_usage(None)
        self.assertIsNone(s["percent"])
        self.assertEqual(u.label_text(s), "n/a")
        self.assertEqual(u.bar_fraction(None), 0.0)
        self.assertEqual(s["source"], "none")
        self.assertIn("n/a", u.tooltip_text(s))

    def test_clamp_over_100(self):                            # bar clamps, number stays real
        s = u.context_usage(1_040_000, "m", 1_000_000)
        self.assertEqual(s["percent"], 104.0)
        self.assertEqual(u.bar_fraction(s["percent"]), 1.0)
        self.assertIn("104%", u.tooltip_text(s))
        self.assertIn("clamped", u.tooltip_text(s))

    def test_bad_window_falls_back(self):
        self.assertEqual(u.context_usage(10, "m", 0)["window_tokens"], u.DEFAULT_WINDOW)

    def test_colour_thresholds(self):                         # green <60, amber 60-85, red >85
        self.assertEqual([u.level(p) for p in (None, 0, 59.9, 60, 85, 85.1, 150)],
                         ["na", "ok", "ok", "warn", "warn", "high", "high"])

    def test_fmt_tokens(self):
        self.assertEqual([u.fmt_tokens(n) for n in (None, 950, 123_000, 1_000_000, 1_500_000)],
                         ["n/a", "950", "123k", "1M", "1.5M"])

    def test_tooltip_text(self):
        t = u.tooltip_text(u.context_usage(123_000, "m", 1_000_000, assumed=False))
        self.assertEqual(t, "Context used: 123k of 1M tokens (12%) — how full this conversation's memory is. "
                            "Restart or 'start fresh' to clear it.")
        self.assertIn("assumed", u.tooltip_text(u.context_usage(1000, "m", 200_000, assumed=True)))


class WindowSize(unittest.TestCase):
    def test_reported_wins(self):
        self.assertEqual(u.window_for_model("claude-sonnet-5-5", {"claude-sonnet-5-5": 1_000_000}), (1_000_000, False))

    def test_reported_alias_suffix(self):                     # "x" vs "x[1m]"
        self.assertEqual(u.window_for_model("claude-x", {"claude-x[1m]": 1_000_000}), (1_000_000, False))

    def test_1m_suffix_override_default(self):
        self.assertEqual(u.window_for_model("claude-x[1m]"), (1_000_000, False))
        self.assertEqual(u.window_for_model("claude-x", override=500_000), (500_000, False))
        self.assertEqual(u.window_for_model("claude-x"), (200_000, True))
        self.assertEqual(u.window_for_model(None), (200_000, True))


class Tracker(unittest.TestCase):
    def test_stream_updates_and_window_from_result(self):
        t = u.UsageTracker()
        self.assertIsNone(t.snapshot()["percent"])
        t.feed(asst(inp=2, cr=100_000, cc=20_000, out=500))
        s = t.snapshot()
        self.assertEqual((s["used_tokens"], s["source"], s["assumed_window"]), (120_502, "stream", True))
        t.feed(result(window=1_000_000))
        s = t.snapshot()
        self.assertEqual((s["window_tokens"], s["percent"], s["assumed_window"]), (1_000_000, 12.1, False))

    def test_reset_new_conversation(self):
        t = u.UsageTracker()
        t.feed(asst(inp=5000))
        t.reset("new")
        self.assertIsNone(t.snapshot()["percent"])
        t.feed(asst(inp=10))
        self.assertEqual(t.snapshot()["used_tokens"], 10)

    def test_model_switch_changes_window(self):               # 'think hard' runs another model
        t = u.UsageTracker()
        t.feed(result("small", 200_000))
        t.feed(result("big", 1_000_000))
        t.feed(asst(inp=100_000, model="small"))
        self.assertEqual(t.snapshot()["percent"], 50.0)
        t.feed(asst(inp=100_000, model="big"))
        self.assertEqual(t.snapshot()["percent"], 10.0)

    def test_compaction_drop(self):
        t = u.UsageTracker()
        t.feed(result())
        t.feed(asst(inp=700_000))
        self.assertEqual(t.snapshot()["percent"], 70.0)
        t.feed({"type": "system", "subtype": "compact_boundary"})
        self.assertEqual(t.snapshot()["percent"], 0.0)
        t.feed(asst(inp=80_000))
        self.assertEqual(t.snapshot()["percent"], 8.0)

    def test_helper_events_do_not_count(self):
        t = u.UsageTracker()
        t.feed(asst(inp=1000))
        t.feed(asst(inp=900_000, parent="toolu_9"))
        self.assertEqual(t.snapshot()["used_tokens"], 1000)

    def test_resume_new_session_id_is_not_a_reset(self):
        t = u.UsageTracker()
        t.feed(asst(inp=1000, sid="a"))
        t.feed({"type": "system", "subtype": "init", "session_id": "b", "model": "claude-sonnet-5-5"})
        self.assertEqual(t.snapshot()["used_tokens"], 1000)

    def test_garbage_events_never_raise(self):
        t = u.UsageTracker()
        for ev in (None, 5, "x", {}, {"type": "result", "modelUsage": "bad"}, {"type": "assistant", "message": 3},
                   {"type": "rate_limit_event"}):
            t.feed(ev)
        self.assertIsNone(t.snapshot()["percent"])

    def test_window_override(self):
        t = u.UsageTracker(window_override=400_000)
        t.feed(asst(inp=100_000))
        self.assertEqual(t.snapshot()["percent"], 25.0)


class Transcript(unittest.TestCase):
    def test_read_tail_and_seed(self):
        with tempfile.TemporaryDirectory() as d:
            proj = pathlib.Path(d, "proj")
            proj.mkdir()
            f = proj / "sess-1.jsonl"
            f.write_text('{"type":"user"}\n' + REAL_TRANSCRIPT_LINE + "\n" + '{"type":"user"}\ngarbage\n', encoding="utf-8")
            self.assertEqual(u.find_transcript("sess-1", d), str(f))
            self.assertEqual(u.read_transcript_usage(str(f)), ("claude-sonnet-5-5", REAL_USED))
            t = u.UsageTracker()
            self.assertTrue(t.load_transcript("sess-1", d))
            s = t.snapshot()
            self.assertEqual((s["used_tokens"], s["source"]), (REAL_USED, "transcript"))

    def test_missing_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(u.find_transcript("nope", d))
            self.assertIsNone(u.find_transcript("../x", d))        # no path tricks
            self.assertIsNone(u.read_transcript_usage(os.path.join(d, "none.jsonl")))
            self.assertFalse(u.UsageTracker().load_transcript("nope", d))

    def test_stream_beats_transcript(self):                   # live numbers are never overwritten by the file
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "p").mkdir()
            pathlib.Path(d, "p", "s.jsonl").write_text(REAL_TRANSCRIPT_LINE + "\n", encoding="utf-8")
            t = u.UsageTracker()
            t.feed(asst(inp=42))
            self.assertFalse(t.load_transcript("s", d))
            self.assertEqual(t.snapshot()["used_tokens"], 42)


class Plan(unittest.TestCase):
    def test_utilisation_fraction_and_percent(self):
        p = u.parse_rate_limit({"rate_limit_info": {"utilization": 0.85, "rateLimitType": "five_hour", "resetsAt": 5}})
        self.assertEqual((p["percent"], p["label"]), (85.0, "5-hour session"))
        self.assertEqual(u.parse_rate_limit({"rate_limit_info": {"utilization": 90}})["percent"], 90.0)

    def test_no_number_means_no_bar(self):                    # sparse by design: never faked
        self.assertIsNone(u.parse_rate_limit({"rate_limit_info": {"status": "allowed"}}))
        self.assertIsNone(u.parse_rate_limit({}))

    def test_plan_expires_and_survives_reset(self):
        now = [1000.0]
        t = u.UsageTracker(clock=lambda: now[0])
        t.feed({"type": "rate_limit_event", "rate_limit_info": {"utilization": 0.5, "resetsAt": 2000}})
        t.reset()
        self.assertEqual(t.plan_snapshot()["percent"], 50.0)
        now[0] = 2001.0
        self.assertIsNone(t.plan_snapshot())

    def test_tooltip_includes_plan(self):
        txt = u.tooltip_text(u.context_usage(1, "m", 100), {"percent": 40.0, "label": "5-hour session", "resets_at": None})
        self.assertIn("Plan limit (5-hour session): 40% used", txt)

    def test_session_label_na_without_plan(self):
        # no rate_limit data -> bar says n/a, tooltip says why, context % only in tooltip
        self.assertEqual(u.plan_label_text(None), "n/a")
        t = u.session_tooltip_text(u.context_usage(500_000, "m", 1_000_000))
        self.assertIn("n/a", t)
        self.assertIn("No number is invented", t)
        self.assertIn("50%", t)

    def test_session_label_with_plan(self):
        plan = {"percent": 63.0, "label": "5-hour session", "resets_at": None}
        self.assertEqual(u.plan_label_text(plan), "63%")
        self.assertIn("Session (5-hour session): 63% used", u.session_tooltip_text(u.context_usage(1, "m", 100), plan))


class Wiring(unittest.TestCase):
    """Source-level checks that the app really feeds and shows the tracker (no heavy imports)."""
    def test_brain_feeds_and_resets(self):
        src = (HERE / "voice_claude.py").read_text(encoding="utf-8-sig")
        self.assertIn("tracker.feed(ev)", src)
        self.assertGreaterEqual(src.count("self.usage.reset()"), 2)

    def test_window_has_bar(self):
        import chat_window as cw
        self.assertTrue(hasattr(cw, "UsageBar"))
        src = (HERE / "chat_window.py").read_text(encoding="utf-8-sig")
        self.assertIn("self._paint_usage()", src)


if __name__ == "__main__":
    unittest.main()


class TestAssumedWindowEscalation(unittest.TestCase):
    """Regression (10 Oct 2026): the bar showed 269% because an ASSUMED 200k window was used with ~538k tokens."""

    def test_assumed_window_is_escalated_when_usage_exceeds_it(self):
        s = u.context_usage(538_000, "sonnet", None, assumed=True)
        self.assertEqual(s["window_tokens"], u.EXTENDED_WINDOW)
        self.assertAlmostEqual(s["percent"], 53.8, places=1)
        self.assertTrue(s["assumed_window"])          # still flagged as a guess in the tooltip

    def test_assumed_window_unchanged_when_usage_fits(self):
        s = u.context_usage(120_000, "sonnet", None, assumed=True)
        self.assertEqual(s["window_tokens"], u.DEFAULT_WINDOW)
        self.assertEqual(s["percent"], 60.0)

    def test_known_window_is_never_escalated(self):
        # A window the CLI or config really reported is trusted: over 100% stays over 100% (clamped by the bar).
        s = u.context_usage(250_000, "x", 200_000, assumed=False)
        self.assertEqual(s["window_tokens"], 200_000)
        self.assertEqual(s["percent"], 125.0)


# Real sample (Claude Code 2.1.295, headless stream-json); ids are harmless.
REAL_RL = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": 1791664200, "rateLimitType": "five_hour", "overageStatus": "rejected",
    "isUsingOverage": False, "unifiedWindows": {
        "five_hour": {"utilization": 0.8, "resetsAt": 1791664200},
        "seven_day": {"utilization": 0.1, "resetsAt": 1792216800},
        "seven_day_overage_included": {"utilization": 0, "resetsAt": 1792216800}}},
    "session_id": "x"}
NOW = 1791660000.0       # before the sample's reset


class TestPlanUnifiedWindows(unittest.TestCase):
    def test_real_sample(self):                               # five_hour is the main figure, seven_day is weekly
        p = u.parse_rate_limit(REAL_RL, NOW)
        self.assertEqual((p["percent"], p["resets_at"], p["weekly_percent"]), (80.0, 1791664200, 10.0))
        self.assertEqual(u.plan_label_text(p), "80%")
        t = u.session_tooltip_text(u.context_usage(1, "m", 100), p)
        self.assertIn("Session (5-hour session): 80% used", t)
        self.assertIn("Weekly: 10% used", t)

    def test_units(self):                                     # <=1 fraction, >1 percent, exactly 1.0 -> 100%
        self.assertEqual([u.to_percent(x) for x in (0, 0.79, 0.5, 1.0, 1, 1.5, 79, 100)],
                         [0.0, 79.0, 50.0, 100.0, 100.0, 1.5, 79.0, 100.0])
        self.assertEqual([u.to_percent(x) for x in (None, -1, "0.5", True, float("nan"))], [None] * 5)

    def test_missing_five_hour_uses_top_level_then_weekly_only(self):
        p = u.parse_rate_limit({"rate_limit_info": {"utilization": 0.3, "resetsAt": 9, "rateLimitType": "five_hour"}})
        self.assertEqual((p["percent"], p["resets_at"], p["weekly_percent"]), (30.0, 9, None))
        w = u.parse_rate_limit({"rate_limit_info": {"unifiedWindows": {"seven_day": {"utilization": 0.2}}}})
        self.assertIsNone(w["percent"])
        self.assertEqual(w["weekly_percent"], 20.0)
        self.assertEqual(u.plan_label_text(w), "n/a")
        self.assertIn("Weekly: 20% used", u.session_tooltip_text(u.context_usage(), w))

    def test_garbage(self):
        for bad in (None, 5, "x", [], {}, {"rate_limit_info": 3}, {"rate_limit_info": {"unifiedWindows": "x"}},
                    {"rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": "high"}}}},
                    {"rate_limit_info": {"status": "allowed"}}):
            self.assertIsNone(u.parse_rate_limit(bad))

    def test_status_only_event_keeps_five_hour(self):
        t = u.UsageTracker(clock=lambda: NOW)
        t.feed(REAL_RL)
        t.feed({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
        t.feed({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"seven_day": {"utilization": 0.2}}}})
        p = t.plan_snapshot()
        self.assertEqual((p["percent"], p["weekly_percent"]), (80.0, 20.0))

    def test_other_event_type_or_nested(self):                # arrives under another name / inside a result
        t = u.UsageTracker(clock=lambda: NOW)
        t.feed({"type": "system", "subtype": "x", "data": REAL_RL})
        self.assertEqual(t.plan_snapshot()["percent"], 80.0)
        t2 = u.UsageTracker(clock=lambda: NOW)
        t2.feed(dict(REAL_RL, type="result"))
        self.assertEqual(t2.plan_snapshot()["percent"], 80.0)
        t3 = u.UsageTracker(clock=lambda: NOW)           # assistant text mentioning it is never parsed
        t3.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "rate_limit_info"}]}})
        self.assertIsNone(t3.plan_snapshot())

    def test_five_hour_expires_weekly_stays(self):
        now = [NOW]
        t = u.UsageTracker(clock=lambda: now[0])
        t.feed(REAL_RL)
        now[0] = 1791664201.0
        p = t.plan_snapshot()
        self.assertIsNone(p["percent"])
        self.assertEqual(p["weekly_percent"], 10.0)
        now[0] = 1792216801.0
        self.assertIsNone(t.plan_snapshot())


class TestPlanPersistence(unittest.TestCase):
    def test_round_trip_and_numbers_only(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "state", "plan_usage.json")
            t = u.UsageTracker(clock=lambda: NOW, plan_path=f)
            t.feed(REAL_RL)
            data = json.load(open(f, encoding="utf-8"))
            self.assertTrue(all(isinstance(v, (int, float)) for v in data.values()))
            self.assertEqual(os.listdir(os.path.dirname(f)), ["plan_usage.json"])      # no temp left behind
            t2 = u.UsageTracker(clock=lambda: NOW + 60, plan_path=f)                   # "restart"
            p = t2.plan_snapshot()
            self.assertEqual((p["percent"], p["weekly_percent"], p["resets_at"]), (80.0, 10.0, 1791664200))

    def test_stale_by_age(self):                              # seen > 6 h ago -> n/a
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "p.json")
            u.save_plan(f, u.parse_rate_limit(dict(REAL_RL), NOW))
            self.assertIsNone(u.load_plan(f, NOW + 6 * 3600 + 1))
            self.assertIsNotNone(u.load_plan(f, NOW + 3600))

    def test_stale_by_reset(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "p.json")
            u.save_plan(f, u.parse_rate_limit(REAL_RL, 1791663000.0))
            p = u.load_plan(f, 1791664300.0)                   # window reset, weekly still valid
            self.assertIsNone(p["percent"])
            self.assertEqual(p["weekly_percent"], 10.0)

    def test_missing_or_garbage_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(u.load_plan(os.path.join(d, "none.json")))
            f = os.path.join(d, "bad.json")
            open(f, "w").write("{not json")
            self.assertIsNone(u.load_plan(f))
            open(f, "w").write('{"seen_at": "x", "percent": "y"}')
            self.assertIsNone(u.load_plan(f))
            self.assertFalse(u.UsageTracker(plan_path=f).plan_snapshot() is not None)
