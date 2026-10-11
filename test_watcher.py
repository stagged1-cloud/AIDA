"""Tests for watcher.py. Run: .venv\\Scripts\\python -m unittest test_watcher -v
Each test is labelled (T01...) and says what it proves. No network, no model, no real screen, temp dirs only."""
import json
import pathlib
import tempfile
import unittest
import datetime as dt

from PIL import Image

import watcher as w

UTC = dt.timezone.utc


def utc(*a):
    return dt.datetime(*a, tzinfo=UTC)


def write(path, text):
    pathlib.Path(path).write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._td.name)
        self.state = self.tmp / "state"
        self.watches = self.tmp / "watches.yaml"

    def tearDown(self):
        self._td.cleanup()

    def make(self, yaml_text, **kw):
        write(self.watches, yaml_text)
        self.sent = []
        kw.setdefault("is_idle", lambda: True)
        kw.setdefault("submit", lambda p, n, d: self.sent.append((n, p, d)) or True)
        return w.Watcher(self.watches, state_dir=self.state, **kw)


# ---------------------------------------------------------------- trigger parsing
class TestParsing(Base):
    def test_t01_valid_file_loads_all_types(self):
        # T01: one of each type parses, defaults applied (enabled true, cooldown 60)
        write(self.watches, """
watches:
  - {name: a, type: timer, time: "17:30", prompt: hi}
  - {name: b, type: file, folder: ".", prompt: hi}
  - {name: c, type: screen, monitor: left, prompt: hi}
""")
        ts, errs = w.parse_watches(self.watches)
        self.assertEqual([t.name for t in ts], ["a", "b", "c"])
        self.assertEqual(errs, [])
        self.assertTrue(all(t.enabled and t.cooldown_s == 60 for t in ts))

    def test_t02_bad_entries_skipped_good_kept(self):
        # T02: each invalid entry is rejected with a reason; the valid one still loads
        write(self.watches, """
- {name: ok, type: timer, every_min: 5, prompt: p}
- {type: timer, every_min: 5, prompt: p}
- {name: notype, type: wibble, prompt: p}
- {name: noprompt, type: timer, every_min: 5}
- {name: badtime, type: timer, time: "25:99", prompt: p}
- {name: ok, type: timer, every_min: 5, prompt: p}
- {name: negcd, type: timer, every_min: 5, prompt: p, cooldown_s: -1}
- {name: badrx, type: file, folder: ., mode: contains, pattern: "(", prompt: p}
- {name: nocolour, type: screen, monitor: left, mode: colour, prompt: p}
- {name: zero, type: timer, every_min: 0, prompt: p}
- just a string
""")
        ts, errs = w.parse_watches(self.watches)
        self.assertEqual([t.name for t in ts], ["ok"])
        self.assertEqual(len(errs), 10)

    def test_t03_malformed_file_never_raises(self):
        # T03: garbage YAML, wrong top-level shape and a missing file all return empty + a reason
        for text in ["{{{ not yaml: [", "42", "watches: 7"]:
            write(self.watches, text)
            ts, errs = w.parse_watches(self.watches)
            self.assertEqual(ts, [])
            self.assertTrue(errs)
        ts, errs = w.parse_watches(self.tmp / "nope.yaml")
        self.assertEqual(ts, [])
        self.assertTrue(errs)

    def test_t04_json_supported(self):
        # T04: a .json watches file works too
        j = self.tmp / "w.json"
        j.write_text(json.dumps({"watches": [{"name": "j", "type": "timer", "every_s": 30, "prompt": "p"}]}))
        ts, errs = w.parse_watches(j)
        self.assertEqual((len(ts), errs), (1, []))

    def test_t05_example_file_valid_and_all_disabled(self):
        # T05: the shipped example parses cleanly and nothing in it is switched on
        ts, errs = w.parse_watches(w.EXAMPLE_WATCHES)
        self.assertEqual(errs, [])
        self.assertGreaterEqual(len(ts), 5)
        self.assertFalse(any(t.enabled for t in ts))

    def test_t06_broken_reload_keeps_last_good_set(self):
        # T06: editing the file into garbage while running keeps the previous triggers
        wt = self.make("watches:\n  - {name: a, type: timer, every_min: 5, prompt: p}\n")
        self.assertEqual(len(wt.triggers), 1)
        write(self.watches, "{{{ broken")
        wt.reload(force=True)
        self.assertEqual(len(wt.triggers), 1)
        self.assertTrue(wt.errors)


# ---------------------------------------------------------------- schedule maths, UK time
class TestSchedule(unittest.TestCase):
    def test_t10_gmt_and_bst_offsets(self):
        # T10: winter is UTC+0, summer is UTC+1
        self.assertEqual(w.uk(utc(2026, 1, 15, 12)).hour, 12)
        self.assertEqual(w.uk(utc(2026, 7, 15, 12)).hour, 13)

    def test_t11_daily_next_today_or_tomorrow(self):
        # T11: 17:30 UK in July (BST) is 16:30 UTC; after it has passed the next one is tomorrow
        n = w.next_daily(utc(2026, 7, 15, 10), 17, 30)
        self.assertEqual(n, utc(2026, 7, 15, 16, 30))
        n2 = w.next_daily(utc(2026, 7, 15, 17), 17, 30)
        self.assertEqual(n2, utc(2026, 7, 16, 16, 30))

    def test_t12_spring_forward_gap_moves_forward(self):
        # T12: 29 Mar 2026, clocks go 01:00->02:00 so 01:30 does not exist: fires at 02:00 BST-equivalent, once
        n = w.next_daily(utc(2026, 3, 29, 0, 0), 1, 30)
        self.assertEqual(w.uk(n).hour, 2)          # shifted to 02:30 BST
        self.assertEqual(n.astimezone(UTC), utc(2026, 3, 29, 1, 30))

    def test_t13_autumn_ambiguous_fires_once_first_occurrence(self):
        # T13: 25 Oct 2026, 01:30 happens twice; we take the first (BST, 00:30 UTC) and next day is a normal 01:30 GMT
        # (compared in UTC: Python deliberately says ambiguous local times never equal times from another tzinfo)
        n = w.next_daily(utc(2026, 10, 24, 23), 1, 30)
        self.assertEqual(n.astimezone(UTC), utc(2026, 10, 25, 0, 30))
        n2 = w.next_daily(n, 1, 30)
        self.assertEqual(n2.astimezone(UTC), utc(2026, 10, 26, 1, 30))

    def test_t14_day_after_dst_change_keeps_wall_time(self):
        # T14: 17:30 stays 17:30 UK across the October change (16:30 UTC -> 17:30 UTC)
        a = w.next_daily(utc(2026, 10, 24, 12), 17, 30)
        b = w.next_daily(a, 17, 30)
        self.assertEqual((a.astimezone(UTC).hour, b.astimezone(UTC).hour), (16, 17))

    def test_t15_weekday_filter_and_fixed_date(self):
        # T15: weekdays [mon] from a Wednesday lands on Monday; a past fixed date gives None
        n = w.next_daily(utc(2026, 7, 15, 10), 9, 0, weekdays=["mon"])
        self.assertEqual(w.uk(n).weekday(), 0)
        self.assertIsNone(w.next_daily(utc(2026, 7, 15, 10), 9, 0, date=dt.date(2026, 7, 1)))
        self.assertIsNotNone(w.next_daily(utc(2026, 7, 15, 10), 9, 0, date=dt.date(2026, 7, 20)))

    def test_t16_roundtrip_every_hour_of_dst_days(self):
        # T16: utc -> uk -> utc is lossless across both change days (tzinfo sanity)
        for day in (dt.datetime(2026, 3, 29), dt.datetime(2026, 10, 25)):
            for h in range(0, 48):
                u = (day + dt.timedelta(hours=h)).replace(tzinfo=UTC)
                self.assertEqual(w.uk(u).astimezone(UTC), u)

    def test_t17_bad_time_strings_rejected(self):
        # T17: invalid HH:MM raises
        for s in ["7", "24:00", "12:60", "abc", ""]:
            with self.assertRaises(ValueError):
                w.parse_hhmm(s)


class TestTimerTriggers(Base):
    def test_t20_daily_reminder_fires_once_at_time(self):
        # T20: due time passes -> delivered once, not again the same day
        wt = self.make("- {name: r, type: timer, time: '17:30', prompt: 'go {detail}'}\n")
        self.assertEqual(wt.tick(utc(2026, 7, 15, 16, 0)), [])
        self.assertEqual(wt.tick(utc(2026, 7, 15, 16, 31)), ["r"])
        self.assertEqual(wt.tick(utc(2026, 7, 15, 16, 40)), [])
        self.assertIn("17:30", self.sent[0][1])

    def test_t21_every_n_minutes(self):
        # T21: interval timer fires after the interval and repeats
        wt = self.make("- {name: e, type: timer, every_min: 30, cooldown_s: 0, prompt: p}\n")
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(minutes=29)), [])
        self.assertEqual(wt.tick(t0 + dt.timedelta(minutes=31)), ["e"])
        self.assertEqual(wt.tick(t0 + dt.timedelta(minutes=62)), ["e"])

    def test_t22_one_shot_date_fires_then_stays_off(self):
        # T22: a dated reminder fires once and never again
        wt = self.make("- {name: d, type: timer, time: '09:00', date: 2026-07-15, prompt: p}\n")
        self.assertEqual(wt.tick(utc(2026, 7, 15, 8, 5)), ["d"])
        self.assertEqual(wt.tick(utc(2026, 7, 16, 8, 5)), [])

    def test_t23_missed_reminder_not_delivered_late(self):
        # T23: if the app was off/busy for over the grace period, the stale reminder is skipped
        wt = self.make("- {name: r, type: timer, time: '17:30', prompt: p}\n")
        wt.tick(utc(2026, 7, 15, 10))
        self.assertEqual(wt.tick(utc(2026, 7, 15, 19, 0)), [])
        self.assertEqual(wt.tick(utc(2026, 7, 16, 16, 31)), ["r"])        # tomorrow still works


# ---------------------------------------------------------------- cooldown, idle gating, quiet hours
class TestGating(Base):
    Y = "- {name: e, type: timer, every_min: 1, cooldown_s: 600, prompt: p}\n"

    def test_t30_cooldown_blocks_refire(self):
        # T30: trips again inside cooldown_s are suppressed, allowed after it
        wt = self.make(self.Y)
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=61)), ["e"])
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=200)), [])
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=300)), [])
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=700)), ["e"])

    def test_t31_cooldown_survives_restart(self):
        # T31: last-fired is persisted in the state dir, so a new Watcher still honours the cooldown
        wt = self.make(self.Y)
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        wt.tick(t0 + dt.timedelta(seconds=61))
        wt2 = w.Watcher(self.watches, state_dir=self.state, submit=lambda *a: True)
        self.assertGreater(wt2.triggers[0].last_fired, 0)

    def test_t32_not_idle_holds_then_delivers(self):
        # T32: while the assistant is busy nothing is submitted; once idle the held prompt goes out
        busy = {"v": True}
        wt = self.make(self.Y.replace("600", "0"), is_idle=lambda: not busy["v"])
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=61)), [])
        self.assertEqual(self.sent, [])
        busy["v"] = False
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=70)), ["e"])
        self.assertEqual(len(self.sent), 1)

    def test_t33_held_prompt_expires(self):
        # T33: a prompt held too long (max_pending_s) is dropped rather than delivered stale
        busy = {"v": True}
        wt = self.make(self.Y.replace("600", "0"), is_idle=lambda: not busy["v"], max_pending_s=100)
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        wt.tick(t0 + dt.timedelta(seconds=61))
        busy["v"] = False
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=300)), [])

    def test_t34_submit_refusal_keeps_pending(self):
        # T34: if the app refuses the turn (race), the trigger stays pending and retries
        ok = {"v": False}
        wt = self.make(self.Y.replace("600", "0"), submit=lambda p, n, d: ok["v"])
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=61)), [])
        ok["v"] = True
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=66)), ["e"])

    def test_t35_one_delivery_per_tick(self):
        # T35: two tripped triggers are delivered one at a time (the assistant is busy after the first)
        wt = self.make("- {name: a, type: timer, every_min: 1, cooldown_s: 0, prompt: p}\n"
                       "- {name: b, type: timer, every_min: 1, cooldown_s: 0, prompt: p}\n")
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(len(wt.tick(t0 + dt.timedelta(seconds=61))), 1)

    def test_t36_disabled_watcher_and_disabled_trigger_do_nothing(self):
        # T36: global off and per-trigger enabled:false both stay silent
        wt = self.make("- {name: a, type: timer, every_min: 1, enabled: false, prompt: p}\n")
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(minutes=5)), [])
        wt2 = self.make("- {name: a, type: timer, every_min: 1, prompt: p}\n", enabled=False)
        self.assertEqual(wt2.tick(t0), [])
        self.assertEqual(wt2.tick(t0 + dt.timedelta(minutes=5)), [])

    def test_t37_quiet_hours_wrap_midnight(self):
        # T37: 23:00-07:00 UK covers late night and early morning, not midday; BST is respected
        q = "23:00-07:00"
        self.assertTrue(w.in_quiet_hours(utc(2026, 7, 15, 22, 30), q))     # 23:30 BST
        self.assertFalse(w.in_quiet_hours(utc(2026, 7, 15, 21, 30), q))    # 22:30 BST
        self.assertTrue(w.in_quiet_hours(utc(2026, 1, 15, 3), q))
        self.assertFalse(w.in_quiet_hours(utc(2026, 1, 15, 7), q))         # 07:00 GMT is out (end exclusive)
        self.assertFalse(w.in_quiet_hours(utc(2026, 1, 15, 12), q))

    def test_t38_quiet_hours_same_day_and_bad_spec(self):
        # T38: non-wrapping range works; empty, equal or malformed specs mean never quiet
        self.assertTrue(w.in_quiet_hours(utc(2026, 1, 15, 13), "12:00-14:00"))
        self.assertFalse(w.in_quiet_hours(utc(2026, 1, 15, 15), "12:00-14:00"))
        for bad in ["", None, "nonsense", "10:00-10:00", "25:00-26:00"]:
            self.assertFalse(w.in_quiet_hours(utc(2026, 1, 15, 3), bad))

    def test_t39_quiet_hours_suppress_delivery(self):
        # T39: nothing is submitted during quiet hours, even when idle; works again afterwards
        wt = self.make(self.Y.replace("600", "0"), quiet_hours="23:00-07:00")
        t0 = utc(2026, 1, 15, 2)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(minutes=5)), [])
        self.assertEqual(self.sent, [])
        t1 = utc(2026, 1, 15, 12)
        wt.tick(t1)
        self.assertEqual(wt.tick(t1 + dt.timedelta(minutes=2)), ["e"])

    def test_t40_poll_exception_isolated(self):
        # T40: a trigger that blows up is logged and the others still fire
        wt = self.make("- {name: bad, type: screen, monitor: left, prompt: p}\n"
                       "- {name: ok, type: timer, every_min: 1, cooldown_s: 0, prompt: p}\n",
                       capture=lambda m: 1 / 0)
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=61)), ["ok"])

    def test_t41_logs_and_state_only_under_state_dir(self):
        # T41: everything written goes under the given state dir (never memory/topics)
        wt = self.make(self.Y.replace("600", "0"))
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        wt.tick(t0 + dt.timedelta(seconds=61))
        self.assertTrue((self.state / "watcher.log").exists())
        self.assertTrue((self.state / "state.json").exists())
        self.assertNotIn("topics", str(w.STATE_DIR))
        self.assertEqual(w.STATE_DIR.parts[-2:], ("state", "watcher"))


# ---------------------------------------------------------------- file watch
class TestFileWatch(Base):
    def test_t50_new_file_trips_after_baseline(self):
        # T50: existing files are the baseline (no trip); a new matching file trips; non-matching ignored
        d = self.tmp / "in"
        d.mkdir()
        write(d / "old.pdf", "x")
        wt = self.make(f"- {{name: f, type: file, folder: '{d.as_posix()}', glob: '*.pdf', cooldown_s: 0, prompt: 'p {{detail}}'}}\n")
        t0 = utc(2026, 7, 15, 9)
        self.assertEqual(wt.tick(t0), [])
        write(d / "note.txt", "x")
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=5)), [])
        write(d / "new.pdf", "x")
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=10)), ["f"])
        self.assertIn("new.pdf", self.sent[0][1])

    def test_t51_contains_pattern_only_on_new_content(self):
        # T51: pattern in existing content is ignored; appended matching text trips; appended non-match does not
        d = self.tmp / "logs"
        d.mkdir()
        write(d / "a.log", "ERROR old\n")
        wt = self.make(f"- {{name: c, type: file, folder: '{d.as_posix()}', glob: '*.log', mode: contains, "
                       f"pattern: 'ERROR \\w+', cooldown_s: 0, prompt: p}}\n")
        t0 = utc(2026, 7, 15, 9)
        self.assertEqual(wt.tick(t0), [])
        with open(d / "a.log", "a") as fh:
            fh.write("all fine\n")
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=5)), [])
        with open(d / "a.log", "a") as fh:
            fh.write("ERROR disk\n")
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=10)), ["c"])
        self.assertIn("ERROR disk", self.sent[0][2])

    def test_t52_missing_folder_is_quiet(self):
        # T52: a folder that does not exist neither trips nor crashes; it works once it appears
        d = self.tmp / "later"
        wt = self.make(f"- {{name: f, type: file, folder: '{d.as_posix()}', cooldown_s: 0, prompt: p}}\n")
        t0 = utc(2026, 7, 15, 9)
        self.assertEqual(wt.tick(t0), [])
        d.mkdir()
        wt.tick(t0 + dt.timedelta(seconds=5))              # baseline of the empty folder
        write(d / "x.txt", "1")
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=10)), ["f"])

    def test_t53_recursive_option(self):
        # T53: subfolder files only count when recursive: true
        d = self.tmp / "r"
        (d / "sub").mkdir(parents=True)
        wt = self.make(f"- {{name: f, type: file, folder: '{d.as_posix()}', recursive: true, cooldown_s: 0, prompt: p}}\n")
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        write(d / "sub" / "deep.txt", "1")
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=5)), ["f"])


# ---------------------------------------------------------------- screen diff, synthetic images
def solid(colour, size=(320, 180)):
    return Image.new("RGB", size, colour)


def with_block(base, box, colour):
    im = base.copy()
    im.paste(colour, box)
    return im


class TestScreen(Base):
    def test_t60_identical_frames_zero_diff(self):
        # T60: identical images give 0
        self.assertEqual(w.screen_diff_fraction(solid((10, 10, 10)), solid((10, 10, 10))), 0.0)

    def test_t61_small_noise_ignored(self):
        # T61: a tiny brightness wobble (JPEG-ish noise / clock tick) stays under the cell threshold
        self.assertEqual(w.screen_diff_fraction(solid((100, 100, 100)), solid((110, 110, 110))), 0.0)

    def test_t62_block_change_measured(self):
        # T62: a quarter-screen white block changes about 25% of cells
        a = solid((0, 0, 0))
        b = with_block(a, (0, 0, 160, 90), (255, 255, 255))
        self.assertAlmostEqual(w.screen_diff_fraction(a, b), 0.25, delta=0.03)

    def test_t63_full_change(self):
        # T63: black to white changes everything
        self.assertEqual(w.screen_diff_fraction(solid((0, 0, 0)), solid((255, 255, 255))), 1.0)

    def test_t64_colour_fraction_region(self):
        # T64: red banner in the top 30% is found by the region check and not by the bottom region
        img = with_block(solid((20, 20, 20)), (0, 0, 320, 54), (210, 25, 25))
        self.assertGreater(w.colour_fraction(img, [200, 30, 30], 45, (0, 0, 1, 0.3)), 0.9)
        self.assertEqual(w.colour_fraction(img, [200, 30, 30], 45, (0, 0.5, 1, 1)), 0.0)

    def test_t65_screen_diff_trigger_via_watcher(self):
        # T65: first frame is baseline, a static frame is silent, a big change trips, and no model/call happens
        frames = [solid((0, 0, 0)), solid((0, 0, 0)), with_block(solid((0, 0, 0)), (0, 0, 320, 90), (255, 255, 255))]
        it = iter(frames)
        wt = self.make("- {name: s, type: screen, monitor: left, min_changed: 0.2, cooldown_s: 0, prompt: 'look {detail}'}\n",
                       capture=lambda m: next(it))
        t0 = utc(2026, 7, 15, 9)
        self.assertEqual(wt.tick(t0), [])
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=5)), [])
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=10)), ["s"])
        self.assertIn("left screen", self.sent[0][1])

    def test_t66_colour_trigger_is_edge_triggered(self):
        # T66: colour appearing trips once; staying red does not retrip; leaving and returning does
        red = with_block(solid((20, 20, 20)), (0, 0, 320, 60), (210, 25, 25))
        dark = solid((20, 20, 20))
        it = iter([dark, red, red, dark, red])
        wt = self.make("- {name: c, type: screen, monitor: right, mode: colour, colour: [200, 30, 30], "
                       "tolerance: 45, min_fraction: 0.1, cooldown_s: 0, prompt: p}\n", capture=lambda m: next(it))
        t0 = utc(2026, 7, 15, 9)
        out = [wt.tick(t0 + dt.timedelta(seconds=5 * i)) for i in range(5)]
        self.assertEqual(out, [[], ["c"], [], [], ["c"]])

    def test_t67_capture_failure_is_quiet(self):
        # T67: a missing monitor (capture returns None) never trips or crashes
        wt = self.make("- {name: s, type: screen, monitor: middle, cooldown_s: 0, prompt: p}\n", capture=lambda m: None)
        t0 = utc(2026, 7, 15, 9)
        self.assertEqual([wt.tick(t0 + dt.timedelta(seconds=i)) for i in range(3)], [[], [], []])

    def test_t68_resolution_change_resets_baseline(self):
        # T68: if the monitor size changes (resolution switch) it re-baselines rather than reporting a change
        it = iter([solid((0, 0, 0), (320, 180)), solid((255, 255, 255), (640, 360))])
        wt = self.make("- {name: s, type: screen, monitor: left, cooldown_s: 0, prompt: p}\n", capture=lambda m: next(it))
        t0 = utc(2026, 7, 15, 9)
        wt.tick(t0)
        self.assertEqual(wt.tick(t0 + dt.timedelta(seconds=5)), [])

    def test_t69_monitor_naming(self):
        # T69: left/middle/right/primary/number map onto monitors sorted by x; impossible names give None
        rects = [(0, 0, 1920, 1080), (1920, 0, 3840, 1080), (3840, 0, 5760, 1080)]
        self.assertEqual(w.pick_monitor(rects, "left"), rects[0])
        self.assertEqual(w.pick_monitor(rects, "middle"), rects[1])
        self.assertEqual(w.pick_monitor(rects, "right"), rects[2])
        self.assertEqual(w.pick_monitor(rects, "2"), rects[1])
        self.assertEqual(w.pick_monitor(rects, "primary"), rects[0])
        self.assertIsNone(w.pick_monitor(rects, "4"))
        self.assertIsNone(w.pick_monitor(rects[:2], "middle"))
        self.assertIsNone(w.pick_monitor([], "left"))


if __name__ == "__main__":
    unittest.main()
