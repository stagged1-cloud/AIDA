"""Tests for screen_text.py (live assistant M3, log only). Run on its own: .venv\\Scripts\\python.exe test_screen_text.py

Fakes for UI Automation, OCR, the grab and the CPU reading throughout. The last class is a real-world
smoke test (skipped if Windows OCR or mss is unavailable): one real OCR pass on a screen grab held in RAM
and a 20 s run with CPU/RAM figures printed. Nothing in here writes screen text or pixels to disk.
"""
import json
import os
import tempfile
import time
import unittest
from collections import Counter, deque
from pathlib import Path

import numpy as np

import screen_text as st

ON = {"screen_watch_enabled": True, "screen_text_enabled": True, "screen_text_ocr_min_interval_s": 0}


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeWatch:
    """Stands in for M2's ScreenWatch: watching flag and change events."""

    def __init__(self):
        self.watching = True
        self.changes = deque()

    def change(self, ts):
        self.changes.append({"ts": ts, "monitor": 1, "fraction": 0.1})

    def recent_changes(self, since=None):
        return [c for c in self.changes if since is None or c["ts"] >= since]


class Focus:
    """Mutable fake UIA focus result."""

    def __init__(self, **kw):
        self.v = {"title": "Ticket 48213 - Zendesk", "rect": (0, 0, 800, 600), "kind": "edit",
                  "text": "", "edit_rect": (0, 500, 800, 600)}
        self.v.update(kw)

    def __call__(self):
        return dict(self.v)


class FakeOcr:
    def __init__(self, text="Ticket #48213 raised by Liz on 03/10/2026"):
        self.text, self.calls, self.frames = text, 0, []

    def __call__(self, frame):
        self.calls += 1
        self.frames.append(frame)
        return self.text


def fake_grab(rect):
    return np.zeros((600, 800, 4), dtype=np.uint8), (0, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.clock, self.watch, self.focus, self.ocr = FakeClock(), FakeWatch(), Focus(), FakeOcr()
        self.cpu = 5.0

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, cfg=None, **kw):
        args = dict(watch=self.watch, focus_fn=self.focus, ocr_fn=self.ocr, grab_fn=fake_grab,
                    cpu_fn=lambda: self.cpu, clock=self.clock, base=self.base)
        args.update(kw)
        return st.ScreenText(dict(ON, **(cfg or {})), **args)

    def files(self):
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*") if p.is_file())

    def shadow(self):
        p = self.base / st.SHADOW_LOG
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class TestGates(Base):
    # G1: off by default: nothing set means not allowed
    def test_disabled_by_default(self):
        self.assertFalse(st.text_allowed({})[0])
        self.assertFalse(st.DEFAULTS["screen_text_enabled"])

    # G2: screen_text_enabled alone is not enough without screen_watch_enabled
    def test_needs_screen_watch(self):
        ok, why = st.text_allowed({"screen_text_enabled": True})
        self.assertFalse(ok)
        self.assertIn("screen watch", why)

    # G3: only log_only is accepted; anything else refuses cleanly (no exception, no thread)
    def test_log_only_enforced(self):
        ok, why = st.text_allowed(dict(ON, screen_text_mode="popup"))
        self.assertFalse(ok)
        self.assertIn("unsupported mode", why)
        s = self.make({"screen_text_mode": "speak"})
        self.assertFalse(s.start())
        self.assertEqual(s.tick(), "not allowed")
        self.assertEqual(self.files(), [])

    # G4: start() with defaults logs why and starts nothing
    def test_start_disabled(self):
        lines = []
        s = st.ScreenText({}, log=lambda w, t: lines.append(t), base=self.base)
        self.assertFalse(s.start())
        self.assertIn("not started", lines[0])
        self.assertFalse(s.running)

    # G5: CPU guard skips the pass, no OCR
    def test_cpu_guard(self):
        self.cpu = 95.0
        s = self.make()
        self.assertEqual(s.tick(), "cpu busy")
        self.assertEqual(self.ocr.calls, 0)

    # G6: M2 paused (game/fullscreen/mute) means no reading at all
    def test_watch_paused(self):
        self.watch.watching = False
        self.assertEqual(self.make().tick(), "watch paused")
        self.assertEqual(self.ocr.calls, 0)

    # G7: password boxes are never read
    def test_password_skipped(self):
        self.focus.v.update(kind="password", text="")
        self.assertEqual(self.make().tick(), "password field")
        self.assertEqual(self.ocr.calls, 0)


class TestSources(Base):
    # S1: UIA document text is used as context with no OCR call
    def test_uia_first(self):
        self.focus.v.update(kind="doc", text="Server GW-ACME-447 down")
        self.watch.change(1000.0)
        s = self.make()
        self.assertEqual(s.tick(), "context updated")
        self.assertEqual(self.ocr.calls, 0)
        self.assertEqual(s.counters["context_uia"], 1)

    # S2: OCR fallback runs when UIA has no screen text and M2 saw a change
    def test_ocr_fallback(self):
        self.watch.change(1000.0)
        s = self.make()
        s.tick()
        self.assertEqual(self.ocr.calls, 1)
        self.assertEqual(s.counters["context_ocr"], 1)

    # S3: no screen change since last OCR means no OCR
    def test_ocr_needs_change(self):
        self.watch.change(1000.0)
        s = self.make()
        s.tick()
        s.tick()
        self.assertEqual(self.ocr.calls, 1)
        self.watch.change(1001.0)
        s.tick()
        self.assertEqual(self.ocr.calls, 2)

    # S4: same text again is skipped (dedupe)
    def test_unchanged_skip(self):
        s = self.make()                      # no watch: every pass may OCR
        s.watch = None
        s.tick()
        self.assertEqual(s.tick(), "context unchanged")
        self.assertEqual(s.counters["skipped_unchanged"], 1)

    # S5: the user's edit box is blanked before OCR so his typing never counts as screen context
    def test_edit_box_masked(self):
        self.watch.change(1000.0)
        self.make().tick()
        f = self.ocr.frames[0]
        self.assertTrue((f[500:600, :, :3] == 255).all())
        tb = st.TITLEBAR_PX_96DPI                      # caption band is blanked too (see TestOtherWindows T1)
        self.assertTrue((f[tb:500, :, :3] == 0).all())

    # S6: big windows are downscaled under the pixel cap before OCR
    def test_pixel_cap(self):
        big = np.zeros((2160, 3840, 4), dtype=np.uint8)
        small = st.limit_pixels(big, 2_100_000)
        self.assertLessEqual(small.shape[0] * small.shape[1], 2_100_000)

    # S7: OCR switched off in config means no OCR, even on change
    def test_ocr_off(self):
        self.watch.change(1000.0)
        self.make({"screen_text_ocr_enabled": False}).tick()
        self.assertEqual(self.ocr.calls, 0)


class TestHintsAndLog(Base):
    def run_typing(self, typed):
        self.watch.change(1000.0)
        s = self.make()
        s.tick()                                   # learns the screen
        self.focus.v["text"] = typed
        return s, s.tick()

    # L1: a near-miss in the focused edit box writes one shadow line in the right format
    def test_hint_logged(self):
        s, out = self.run_typing("Hi Liz, ticket 48123 is done ")
        self.assertEqual(out, "hint logged")
        rows = self.shadow()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(set(r), {"ts", "title", "typed", "entity", "kind", "confidence"})
        self.assertEqual((r["typed"], r["entity"], r["kind"]), ("48123", "#48213", "id_digits_transposed"))

    # L2: focus-aware: text in a non-edit control is never treated as typing
    def test_not_edit_no_hint(self):
        self.focus.v.update(kind=None, text="ticket 48123 ")
        self.watch.change(1000.0)
        s = self.make()
        s.tick()
        s.tick()
        self.assertEqual(self.shadow(), [])

    # L3: correct typing logs nothing
    def test_correct_no_hint(self):
        self.run_typing("ticket 48213 done on 03/10/2026 ")
        self.assertEqual(self.shadow(), [])

    # L4: nothing but the shadow log is ever written, and no full screen text is in it
    def test_only_shadow_written(self):
        self.ocr.text = "SECRET-PATIENT-TEXT Ticket #48213 " + "filler " * 200
        self.run_typing("ticket 48123 ")
        self.assertEqual(self.files(), [str(st.SHADOW_LOG)])
        raw = (self.base / st.SHADOW_LOG).read_text(encoding="utf-8")
        self.assertNotIn("SECRET-PATIENT-TEXT", raw)
        self.assertNotIn("filler", raw)

    # L5: the same keystrokes again do not produce a second line
    def test_typed_dedupe(self):
        s, _ = self.run_typing("ticket 48123 ")
        s.tick()
        self.assertEqual(len(self.shadow()), 1)

    # L6: long fields are capped at 60 characters
    def test_caps(self):
        from context_watch import Hint
        rec = st.append_shadow(self.base / st.SHADOW_LOG, Hint("t", "a" * 99, "b" * 99, "id", 0.9))
        self.assertEqual((len(rec["typed"]), len(rec["entity"])), (60, 60))

    # L7: summary counts per day, kind and app
    def test_summary(self):
        p = self.base / st.SHADOW_LOG
        p.parent.mkdir()
        rows = [{"ts": "2026-10-10T10:00:00", "title": "T1 - Zendesk", "kind": "id_digits_transposed"},
                {"ts": "2026-10-10T11:00:00", "title": "Chat - Teams", "kind": "host_one_character"},
                {"ts": "2026-10-11T09:00:00", "title": "T2 - Zendesk", "kind": "id_digits_transposed"}]
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
        s = st.summarise(p)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["unreadable"], 1)
        self.assertEqual(s["per_day"], {"2026-10-10": 2, "2026-10-11": 1})
        self.assertEqual(s["per_kind"]["id_digits_transposed"], 2)
        self.assertEqual(s["per_app"]["Zendesk"], 2)

    # L8: summary of a missing log is empty, not an error
    def test_summary_missing(self):
        self.assertEqual(st.summarise(self.base / "nope.jsonl")["total"], 0)

    # L9: stop() wipes the in-RAM context
    def test_stop_wipes(self):
        s, _ = self.run_typing("ticket 48123 ")
        s.stop()
        self.assertEqual(s.tracker.entities(), [])


REF_A = ("Ticket INC-482917 raised on server GW-LAB-447 (10.20.30.41) on 11/10/2026 "
         "for customer Northgate Imaging")
TYPED_B = "Please check ticket INC-482971 on server GW-LAB-474 for Northgate Imagng ."


class FakeWindows:
    """Other top-level windows (z-order, top first) and their UIA text, keyed by hwnd."""

    def __init__(self, wins, texts):
        self.wins, self.texts, self.list_calls, self.text_calls = wins, texts, 0, Counter()

    def list(self):
        self.list_calls += 1
        return [dict(w) for w in self.wins]

    def text(self, hwnd):
        self.text_calls[hwnd] += 1
        return self.texts.get(hwnd, "")


class TestOtherWindows(Base):
    """Regression for the 10 Oct live test: reference text in Notepad A, near-misses typed in Notepad B."""

    def setUp(self):
        super().setUp()
        # Notepad B is focused; Windows echoes the typed text into its title
        self.focus = Focus(title="*Please check ticket INC-482971 on s - Notepad", hwnd=200,
                           rect=(1000, 0, 1800, 600), kind="edit", text=TYPED_B, edit_rect=(1000, 80, 1800, 600))
        self.ocr = FakeOcr(text="")
        self.wins = FakeWindows([{"hwnd": 200, "title": self.focus.v["title"], "rect": (1000, 0, 1800, 600)},
                                 {"hwnd": 100, "title": "refs.txt - Notepad", "rect": (0, 0, 900, 700)}],
                                {100: REF_A})

    def mk(self, cfg=None, **kw):
        kw.setdefault("windows_fn", self.wins.list)
        kw.setdefault("window_text_fn", self.wins.text)
        return self.make(cfg, **kw)

    def kinds(self):
        return sorted(r["kind"] for r in self.shadow())

    # T1: OCR of the focused window blanks the caption, where Notepad echoes his typing
    def test_t1_focused_title_bar_masked(self):
        self.focus.v.update(rect=(0, 0, 800, 600), edit_rect=(0, 80, 800, 600))   # fake_grab origin is (0, 0)
        self.watch.change(1000.0)
        self.make().tick()
        f = self.ocr.frames[0]
        self.assertTrue((f[:st.TITLEBAR_PX_96DPI, :, :3] == 255).all())

    # T2: the exact false positive from the live test is suppressed: OCR of B's own title gives
    #     'NC-482971' (OCR dropped the I); comparing his typing with it must not produce a hint
    def test_t2_self_title_false_positive_suppressed(self):
        self.ocr.text = "*Please check ticket NC-482971 on s - Notepad"
        self.watch.change(1000.0)
        self.assertNotEqual(self.make().tick(), "hint logged")
        self.assertEqual(self.shadow(), [])

    # T3: typed-prefix self-match guard in the tracker itself (any source)
    def test_t3_typed_prefix_self_match_dropped(self):
        from context_watch import ContextTracker
        t = ContextTracker(clock=self.clock)
        t.observe("title", "Please check ticket INC-48297 on s")      # a truncated echo of the typing
        self.assertEqual(t.check_typed("B", "Please check ticket INC-482971 ."), [])

    # T4: the other window is read via UIA and all three near-misses are logged
    def test_t4_three_near_misses_from_other_window(self):
        self.assertEqual(self.mk().tick(), "hint logged")
        recs = {r["typed"]: r for r in self.shadow()}
        self.assertEqual(recs["INC-482971"]["entity"], "INC-482917")
        self.assertEqual(recs["GW-LAB-474"]["entity"], "GW-LAB-447")
        self.assertEqual(recs["Northgate Imagng"]["entity"], "Northgate Imaging")
        self.assertEqual(len(recs), 3)
        self.assertEqual(self.wins.text_calls[200], 0)               # focused window never read as context

    # T5: exact matches stay quiet
    def test_t5_exact_match_quiet(self):
        self.focus.v["text"] = "Please check ticket INC-482917 on server GW-LAB-447 for Northgate Imaging ."
        self.mk().tick()
        self.assertEqual(self.shadow(), [])

    # T6: the reference arrives AFTER the typing finished: the typed text is re-checked
    def test_t6_late_reference_rechecks_typing(self):
        self.wins.texts = {}
        s = self.mk()
        s.tick()
        self.assertEqual(self.shadow(), [])
        self.wins.texts = {100: REF_A}
        self.clock.t += 60
        self.assertEqual(s.tick(), "hint logged")
        self.assertEqual(len(self.shadow()), 3)

    # T7: per-window cache: UIA re-read only after the interval; unchanged text is not re-observed
    def test_t7_uia_cache(self):
        s = self.mk({"screen_text_other_uia_interval_s": 10})
        s.tick()
        s.tick()
        self.assertEqual(self.wins.text_calls[100], 1)
        self.clock.t += 11
        s.tick()
        self.assertEqual(self.wins.text_calls[100], 2)
        self.assertEqual(s.counters["context_window_uia"], 1)          # same text: digest says unchanged

    # T8: cap on the number of other windows; sensitive titles skipped
    def test_t8_cap_and_sensitive(self):
        wins = [{"hwnd": 200, "title": "B", "rect": (1000, 0, 1800, 600)},
                {"hwnd": 1, "title": "Bitwarden", "rect": (0, 0, 500, 500)}]
        wins += [{"hwnd": 10 + i, "title": f"w{i}", "rect": (0, 0, 500, 500)} for i in range(8)]
        fw = FakeWindows(wins, {})
        s = self.make({"screen_text_other_max": 3}, windows_fn=fw.list, window_text_fn=fw.text)
        s.tick()
        self.assertEqual(sorted(fw.text_calls), [10, 11, 12])
        self.assertEqual(s.counters["skipped_sensitive_window"], 1)

    # T9: OCR fallback for a window with no UIA text: one window per pass, focused window's pixels blanked
    def test_t9_ocr_fallback_masks_focused(self):
        self.wins.texts = {}
        self.ocr.text = REF_A
        seen = []

        def grab(rect):
            seen.append(rect)
            return np.zeros((rect[3] - rect[1], rect[2] - rect[0], 4), dtype=np.uint8), (rect[0], rect[1])
        self.watch.change(1000.0)
        s = self.mk(grab_fn=grab, cfg={"screen_text_ocr_min_interval_s": 30})
        s.tick()
        # focused OCR ran first on B's rect, then A's rect; A's frame has B's overlap (x>=1000) blanked
        self.assertIn((0, 0, 900, 700), seen)
        self.assertEqual(s.counters["window_ocr"], 1)
        self.assertEqual(len(self.shadow()), 3)

    # T10: windows that vanish drop out of the cache
    def test_t10_cache_pruned(self):
        s = self.mk()
        s.tick()
        self.assertIn(100, s._win_cache)
        self.wins.wins = self.wins.wins[:1]
        s.tick()
        self.assertNotIn(100, s._win_cache)

    # T11: off switch: screen_text_other_windows false reads no other window
    def test_t11_switch_off(self):
        self.mk({"screen_text_other_windows": False}).tick()
        self.assertEqual(self.wins.list_calls, 0)


def _real_ok():
    try:
        st.WinOcr()
        import mss  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(os.name == "nt" and _real_ok(), "Windows OCR or mss not available")
class TestRealSmoke(unittest.TestCase):
    """Honest numbers. Budget (09_performance_budget.md): wake-up burst <= 15% of one core for <= 2 s,
    at most once per 30 s, <= 300 MB burst; watcher average <= 1% of one core."""

    # R1: one real OCR pass on a full monitor held in RAM; prints ms and character count only
    def test_one_ocr_pass(self):
        import mss
        ocr = st.WinOcr()
        with getattr(mss, "MSS", mss.mss)() as s:
            res = []
            for i, m in enumerate(s.monitors[1:], 1):
                sh = s.grab(m)
                a = np.frombuffer(sh.raw, np.uint8).reshape(sh.height, sh.width, 4)
                ocr(a)                                   # warm-up
                t = time.perf_counter()
                n = len(ocr(a))
                res.append((i, a.shape[1], a.shape[0], round((time.perf_counter() - t) * 1000), n))
        for r in res:
            print(f"\n  monitor {r[0]} {r[1]}x{r[2]}: OCR {r[3]} ms, {r[4]} chars (text discarded)")
        self.assertTrue(res)

    # R2: 20 s of the real tier at a 2 s interval (real UIA, OCR, grab) with CPU and RAM measured
    def test_twenty_seconds(self):
        import psutil
        proc = psutil.Process()
        with tempfile.TemporaryDirectory() as d:
            rss0 = proc.memory_info().rss
            s = st.ScreenText(dict(ON, screen_text_ocr_min_interval_s=2), watch=None, base=Path(d))
            s._make_sources()
            c0, t0, peak = proc.cpu_times(), time.perf_counter(), rss0
            burst = []
            while time.perf_counter() - t0 < 20:
                b = time.perf_counter()
                s.tick()
                burst.append(time.perf_counter() - b)
                peak = max(peak, proc.memory_info().rss)
                time.sleep(max(0, 2 - (time.perf_counter() - b)))
            c1, wall = proc.cpu_times(), time.perf_counter() - t0
            cpu = (c1.user + c1.system - c0.user - c0.system) / wall * 100
            files = [p for p in Path(d).rglob("*") if p.is_file() and p.name != "context_shadow.jsonl"]
        print(f"\n  20 s run: {len(burst)} passes, {s.counters['ocr']} OCR, avg pass {np.mean(burst)*1000:.0f} ms, "
              f"max pass {max(burst)*1000:.0f} ms, CPU {cpu:.1f}% of one core, RAM +{(peak-rss0)/2**20:.0f} MB "
              f"(peak RSS {peak/2**20:.0f} MB)")
        print("  budget: avg <= 1% of one core when idle; OCR every 2 s is far denser than the 30 s burst "
              "budget, so this is a worst case, not the default duty cycle")
        self.assertEqual(files, [])


class TestArgumentErrorRegression(unittest.TestCase):
    """10 Oct 23:32: every pass failed with ctypes.ArgumentError because face3d set argtypes on the SHARED
    ctypes.windll.user32 (GetAncestor = [HWND, UINT]) and uiautomation calls GetAncestor(c_void_p, c_int)."""

    def test_1_shared_argtypes_shape_raises(self):
        # 1. Reproduces the failing call shape on a private DLL instance (no real window needed).
        import ctypes
        from ctypes import wintypes as wt
        f = ctypes.WinDLL("user32").GetAncestor
        f.argtypes, f.restype = [wt.HWND, wt.UINT], wt.HWND
        with self.assertRaises(ctypes.ArgumentError):
            f(ctypes.c_void_p(0x7FFF_0000_1234), ctypes.c_int(2))   # 64-bit handle + c_int flag, as uiautomation does

    def test_2_face3d_leaves_shared_windll_alone(self):
        # 2. Importing face3d must not change argtypes on ctypes.windll (that is what broke uiautomation).
        import ctypes
        try:
            import face3d  # noqa: F401
        except Exception as e:
            self.skipTest(f"face3d not importable here: {type(e).__name__}")
        for name in ("GetAncestor", "SetWindowPos", "IsWindowVisible", "GetWindowRect"):
            self.assertIsNone(getattr(ctypes.windll.user32, name).argtypes, name)
        # and uiautomation's call shape still works on the shared object
        ctypes.windll.user32.GetAncestor(ctypes.c_void_p(0), ctypes.c_int(2))

    def test_3_describe_error_has_message_and_location(self):
        # 3. The log line names the type, message and innermost file:line.
        try:
            raise ValueError("bad arg")
        except ValueError as e:
            s = st.describe_error(e)
        self.assertTrue(s.startswith("ValueError: bad arg at test_screen_text.py:"), s)

    def test_4_backoff_grows_and_caps(self):
        # 4. Backoff doubles from 5 s and caps at 5 minutes.
        self.assertEqual([st.fail_backoff(n) for n in (1, 2, 3, 4)], [5, 10, 20, 40])
        self.assertEqual(st.fail_backoff(50), st.FAIL_BACKOFF_MAX_S)

    def test_5_persistent_failure_logs_once_not_every_pass(self):
        # 5. A focus source that always raises: 10 passes log only FAIL_QUIET_AFTER lines, sleeps back off.
        logs, sleeps = [], []
        s = st.ScreenText(cfg=ON, log=lambda w, t: logs.append(t), cpu_fn=lambda: 0,
                          focus_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                          ocr_fn=lambda f: "", grab_fn=lambda r: (None, (0, 0)))

        def fake_sleep(sec):
            sleeps.append(sec)
            if len([x for x in sleeps if x >= 5]) >= 10:
                s._stop.set()
        s._sleep = fake_sleep
        s._loop()
        fails = [l for l in logs if "pass failed" in l]
        self.assertEqual(len(fails), st.FAIL_QUIET_AFTER)
        self.assertIn("RuntimeError: boom at", fails[0])
        self.assertIn("backing off", fails[-1])
        self.assertEqual(s.counters["errors"], 10)
        self.assertEqual(max(sleeps), st.FAIL_BACKOFF_MAX_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
