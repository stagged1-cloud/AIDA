"""Tests for winctl.py. Run: .venv\\Scripts\\python.exe -m unittest test_winctl -v

Pure tests need no UI. Live tests open their OWN Notepad and kill it by PID afterwards;
they touch no other app. If Notepad cannot be isolated as a new process they are skipped.
"""
import json
import subprocess
import time
import unittest

import winctl
from winctl import WinctlError


class TestSelectorParsing(unittest.TestCase):
    def test_bare_word(self):  # bare word = name-contains or id-equals
        self.assertEqual(winctl.parse_selector("Equals"), {"any": "Equals"})

    def test_key_value_pairs(self):
        self.assertEqual(winctl.parse_selector("name=Equals,type=Button"),
                         {"name": "Equals", "type": "Button"})

    def test_contains_and_index(self):
        self.assertEqual(winctl.parse_selector("name~=Num, index=2"), {"name~": "Num", "index": 2})

    def test_quoted_value_with_comma(self):
        self.assertEqual(winctl.parse_selector("name='a,b',id=x"), {"name": "a,b", "id": "x"})

    def test_empty_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(WinctlError) as cm:
                winctl.parse_selector(bad)
            self.assertEqual(cm.exception.code, "bad_selector")

    def test_unknown_key_rejected(self):
        with self.assertRaises(WinctlError):
            winctl.parse_selector("colour=red")

    def test_bad_index_rejected(self):
        for bad in ("index=x", "name=a,index=-1"):
            with self.assertRaises(WinctlError):
                winctl.parse_selector(bad)

    def test_unterminated_quote_rejected(self):
        with self.assertRaises(WinctlError):
            winctl.parse_selector("name='oops,id=x")

    def test_matching(self):
        sel = winctl.parse_selector("name=Two,type=Button")
        self.assertTrue(winctl.selector_matches(sel, "two", "num2Button", "ButtonControl", "Button"))
        self.assertFalse(winctl.selector_matches(sel, "Two", "x", "TextControl", ""))
        self.assertFalse(winctl.selector_matches({"any": "num"}, "x", "num2Button", "T", ""))  # id must equal, not contain
        self.assertTrue(winctl.selector_matches({"any": "num2button"}, "x", "num2Button", "T", ""))
        self.assertTrue(winctl.selector_matches({"any": "wo"}, "Two", "", "T", ""))  # name contains

    def test_window_selector(self):
        self.assertEqual(winctl.parse_window_selector("hwnd:0x10"), {"hwnd": 16})
        self.assertEqual(winctl.parse_window_selector("pid:42"), {"pid": 42})
        self.assertEqual(winctl.parse_window_selector("process:Notepad"), {"process": "notepad"})
        self.assertEqual(winctl.parse_window_selector("Calculator"), {"title": "Calculator"})
        with self.assertRaises(WinctlError):
            winctl.parse_window_selector("pid:abc")


class TestDpiMaths(unittest.TestCase):
    # Mocked three-monitor layout: left 1080p @100%, middle 4K @150%, right 1080p @100%
    RAW = [
        {"device": "R", "rect": [3840, 0, 5760, 1080], "primary": False, "dpi": 96},
        {"device": "L", "rect": [0, 0, 1920, 1080], "primary": False, "dpi": 96},
        {"device": "M", "rect": [1920, 0, 5760 - 1920, 2160], "primary": True, "dpi": 144},
    ]

    def setUp(self):
        self.mons = winctl.number_monitors(self.RAW)

    def test_numbering_left_to_right(self):
        self.assertEqual([m["device"] for m in self.mons], ["L", "M", "R"])
        self.assertEqual([m["label"] for m in self.mons], ["left", "middle", "right"])
        self.assertEqual([m["number"] for m in self.mons], [1, 2, 3])

    def test_scale(self):
        self.assertEqual(self.mons[1]["scale"], 1.5)
        self.assertEqual(self.mons[0]["scale"], 1.0)
        self.assertEqual(winctl.dpi_scale(192), 2.0)
        self.assertEqual(winctl.dpi_scale(0), 1.0)  # unknown dpi -> 96

    def test_logical_physical_roundtrip(self):
        self.assertEqual(winctl.logical_to_physical(800, 1.5), 1200)
        self.assertEqual(winctl.physical_to_logical(1200, 1.5), 800)

    def test_monitor_for_rect(self):
        self.assertEqual(winctl.monitor_for_rect([100, 100, 500, 500], self.mons), 1)
        self.assertEqual(winctl.monitor_for_rect([2000, 100, 2400, 500], self.mons), 2)
        # straddles left/middle boundary, 100px on left, 300px on middle -> middle
        self.assertEqual(winctl.monitor_for_rect([1820, 100, 2220, 500], self.mons), 2)
        # fully off-screen -> None
        self.assertIsNone(winctl.monitor_for_rect([9000, 0, 9100, 100], self.mons))

    def test_rect_relative(self):
        self.assertEqual(winctl.rect_relative_to_monitor([2000, 50, 2400, 450], [1920, 0, 3840, 2160]),
                         [80, 50, 480, 450])

    def test_two_monitor_and_single_labels(self):
        two = winctl.number_monitors(self.RAW[:2])
        self.assertEqual([m["label"] for m in two], ["left", "right"])
        four = winctl.number_monitors(self.RAW + [{"rect": [6000, 0, 7000, 1000], "dpi": 96, "primary": False}])
        self.assertEqual(four[3]["label"], "monitor4")


class TestOutputShape(unittest.TestCase):
    KEYS = {"ok", "command", "method", "real_input_used", "cursor_touched", "cursor_before", "cursor_after"}

    def test_envelope_untouched(self):
        r = winctl.result("x", True, "m", (1, 2), (1, 2))
        self.assertTrue(self.KEYS <= set(r))
        self.assertFalse(r["cursor_touched"])
        json.dumps(r)

    def test_envelope_cursor_moved(self):
        self.assertTrue(winctl.result("x", True, "m", (1, 2), (3, 4))["cursor_touched"])

    def test_envelope_real_input_flag(self):  # real input counts as touched even if cursor restored
        r = winctl.result("x", True, "real_mouse_click", (1, 2), (1, 2), real_input_used=True)
        self.assertTrue(r["cursor_touched"])

    def test_invalid_window_report(self):
        r = winctl.run(["read", "zz_no_such_window_zz", "id=a"])
        self.assertTrue(self.KEYS <= set(r))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "window_not_found")
        self.assertFalse(r["cursor_touched"])

    def test_bad_selector_report(self):
        r = winctl.run(["click", "zz_no_such_window_zz", ""])
        self.assertFalse(r["ok"])
        self.assertIn(r["error"], ("bad_selector", "window_not_found"))

    def test_list_windows_shape(self):
        r = winctl.run(["list-windows"])
        self.assertTrue(r["ok"])
        self.assertTrue(r["monitors"])
        for w in r["windows"][:3]:
            self.assertTrue({"title", "process", "hwnd", "pid", "monitor", "rect"} <= set(w))

    def test_bad_monitor(self):
        r = winctl.run(["screenshot-monitor", "99", "unused.png"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "monitor_not_found")


def _notepad_pids():
    return {w["pid"] for w in winctl.list_windows() if (w["process"] or "").lower() == "notepad.exe"}


def _cursor_is_quiet(samples=6, gap=0.05):
    """True if the real cursor holds still over a short window (the user is not using the mouse right now)."""
    first = winctl.get_cursor()
    for _ in range(samples):
        time.sleep(gap)
        if winctl.get_cursor() != first:
            return False
    return True


def mouse_sensitive(fn):
    """For tests that prove the tool never moves the real cursor. A human moving the mouse while they run proves
    nothing either way, so: skip up front if the mouse is moving, and if an assertion fails while the mouse is
    STILL moving, skip with a clear message. A failure with a stationary mouse is still a real failure."""
    import functools

    @functools.wraps(fn)
    def wrapper(self):
        if not _cursor_is_quiet():
            self.skipTest("real mouse was moving before the app action started; rerun with the mouse still")
        try:
            return fn(self)
        except AssertionError:
            if not _cursor_is_quiet():
                self.skipTest("real mouse was moved during the test; rerun with the mouse still")
            raise
    return wrapper


class TestLiveNotepad(unittest.TestCase):
    """Opens its own Notepad; touches nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.pre = _notepad_pids()
        subprocess.Popen(["notepad.exe"])
        cls.pid = None
        for _ in range(40):
            time.sleep(0.25)
            new = _notepad_pids() - cls.pre
            if new:
                cls.pid = new.pop()
                break
        if cls.pid is None:
            raise unittest.SkipTest("could not isolate a new Notepad process")
        time.sleep(1.0)
        cls.w = f"pid:{cls.pid}"

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "pid", None) and cls.pid not in cls.pre:
            subprocess.run(["taskkill", "/PID", str(cls.pid), "/F"], capture_output=True)

    @mouse_sensitive
    def test_cursor_untouched_type_read_screenshot(self):
        before = winctl.get_cursor()
        r1 = winctl.run(["type", self.w, "type=Document", "hello winctl"])
        r2 = winctl.run(["read", self.w, "type=Document"])
        r3 = winctl.run(["type", self.w, "type=Document", " more", "--append"])
        r4 = winctl.run(["read", self.w, "name=Text editor"])
        r5 = winctl.run(["tree", self.w, "--depth", "2"])
        after = winctl.get_cursor()
        self.assertEqual(before, after)  # real mouse never moved
        for r in (r1, r2, r3, r4, r5):
            self.assertTrue(r["ok"], r)
            self.assertFalse(r["cursor_touched"])
            self.assertFalse(r["real_input_used"])
        self.assertEqual(r1["method"], "uia_ValuePattern")
        self.assertEqual(r2["text"], "hello winctl")
        self.assertEqual(r4["text"], "hello winctl more")

    def test_missing_control(self):
        r = winctl.run(["click", self.w, "id=DoesNotExist"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "control_not_found")

    def test_screenshot_window(self):
        import os, tempfile
        out = os.path.join(tempfile.gettempdir(), "winctl_test_np.png")
        r = winctl.run(["screenshot-window", self.w, out])
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.getsize(out) > 1000)
        self.assertFalse(r["cursor_touched"])


class TestLiveCalculator(unittest.TestCase):
    """Opens its own Calculator (killed by PID afterwards): 12*12 via InvokePattern, cursor untouched."""

    @staticmethod
    def _calc_pids():
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CalculatorApp.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        return {int(l.split('","')[1]) for l in out.splitlines() if l.startswith('"Calc')}

    @classmethod
    def setUpClass(cls):
        cls.pre = cls._calc_pids()
        if cls.pre:
            raise unittest.SkipTest("a Calculator is already running; not touching it")
        subprocess.Popen(["calc.exe"])
        cls.pids = set()
        for _ in range(40):
            time.sleep(0.25)
            cls.pids = cls._calc_pids()
            if cls.pids and any(w["title"] == "Calculator" for w in winctl.list_windows()):
                break
        if not cls.pids:
            raise unittest.SkipTest("Calculator did not start")
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        for p in getattr(cls, "pids", ()):
            subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True)

    @mouse_sensitive
    def test_12_times_12(self):
        before = winctl.get_cursor()
        for sel in ("id=clearButton", "id=num1Button", "id=num2Button", "id=multiplyButton",
                    "id=num1Button", "id=num2Button", "id=equalButton"):
            r = winctl.run(["click", "title-placeholder" and "Calculator", sel])
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["method"], "uia_InvokePattern")
            self.assertFalse(r["cursor_touched"])
        r = winctl.run(["read", "Calculator", "id=CalculatorResults"])
        self.assertEqual(r["text"], "Display is 144")
        self.assertEqual(before, winctl.get_cursor())

    @mouse_sensitive
    def test_failure_case_readonly_display(self):  # display ignores typing: clean fallback report
        r = winctl.run(["type", "Calculator", "id=CalculatorResults", "5"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "no_uia_method")
        self.assertFalse(r["cursor_touched"])


class TestFallbackReporting(unittest.TestCase):
    """Deliberate failure: a control with no usable UIA pattern -> clean fallback report,
    nothing done, cursor untouched. Uses fakes so no other app is involved."""

    class FakeCtl:
        Name, AutomationId, ControlTypeName, ClassName = "ghost", "g", "PaneControl", ""
        NativeWindowHandle = 0

    def run_cmd(self, fn_name, argv):
        fake = self.FakeCtl()
        orig = (winctl.resolve_window, winctl.root_control, winctl.find_control, winctl.supported_patterns)
        winctl.resolve_window = lambda s: ({"title": "t", "process": "p", "pid": 1, "hwnd": 1,
                                            "monitor": 1, "minimised": False}, 1)
        winctl.root_control = lambda w: None
        winctl.find_control = lambda r, s, d: fake
        winctl.supported_patterns = lambda c: []
        try:
            return winctl.run(argv)
        finally:
            winctl.resolve_window, winctl.root_control, winctl.find_control, winctl.supported_patterns = orig

    def test_click_without_flag_reports_fallback(self):
        before = winctl.get_cursor()
        r = self.run_cmd("click", ["click", "x", "id=g"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "no_uia_method")
        self.assertEqual(r["fallback_available"], "real_mouse_click")
        self.assertFalse(r["cursor_touched"])
        self.assertEqual(before, winctl.get_cursor())

    def test_type_without_flag_reports_fallback(self):
        r = self.run_cmd("type", ["type", "x", "id=g", "hi"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["fallback_available"], "real_keyboard")
        self.assertFalse(r["cursor_touched"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
