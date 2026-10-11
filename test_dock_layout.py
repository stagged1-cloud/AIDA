"""Dock/helper-panel layout regression tests (18:47 10 Oct 2026 'head vanishes when helper panel shows' report).
Investigation result: the layout is fine (dock column keeps its rect, panel never overlaps it, render never pauses);
the 'missing head' came from winctl's PrintWindow capture omitting GDI-blitted pixels. These tests lock that in.
Real Tk windows, but no GL: the head itself is replaced by a fake face. Skipped when no display is available."""
import time
import tkinter as tk
import unittest

import chat_window as cw
import head_dock


class FakeFace:
    docked = True

    def __init__(self):
        self.paused_calls = []

    def set_paused(self, p):
        self.paused_calls.append(p)


class Ov:
    face = None


class App:
    state = {"chat_pos": [10, 10], "chat_size": [1200, 800]}
    working = False

    def __init__(self):
        self.agents, self.overlay = {}, Ov()

    def status(self):
        return ("Idle", "", None)

    def text_turn(self, *a):
        pass


def rect(w):
    return (w.winfo_rootx(), w.winfo_rooty(), w.winfo_rootx() + w.winfo_width(), w.winfo_rooty() + w.winfo_height())


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class DockLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"no display: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.app = App()
        self.cw = cw.ChatWindow(self.root, self.app, single=True)
        self.face = FakeFace()
        self.cw.dock_frame(260)
        self.cw.dock_ctl.sync(True, self.face)
        self.cw.win.update()

    def tearDown(self):
        try:
            self.cw.win.destroy()
        except Exception:
            pass

    def dock_rect(self):
        self.cw.win.update()
        return rect(self.cw.dock.frame)

    def start_helper(self, aid="a1"):
        self.app.agents[aid] = {"title": "T", "status": "Working", "started": time.time(), "feed": [("x", "step")]}
        self.cw._paint_panel()

    def test_dock_rect_unchanged_when_panel_shows(self):
        # Layout: dock rect is non-empty and identical before/after the helper panel appears.
        before = self.dock_rect()
        self.assertGreater(before[2] - before[0], 100)
        self.start_helper()
        self.assertTrue(self.cw._panel_shown)
        self.assertEqual(before, self.dock_rect())

    def test_panel_never_overlaps_dock(self):
        # Layout: panel rect and dock rect are disjoint when expanded and when collapsed.
        self.start_helper()
        self.cw.win.update()
        self.assertFalse(overlap(rect(self.cw.panel_frame), self.dock_rect()))
        self.cw.toggle_panel()
        self.cw.win.update()
        self.assertFalse(overlap(rect(self.cw.panel_frame), self.dock_rect()))
        self.assertGreater(self.dock_rect()[3] - self.dock_rect()[1], 100)

    def test_dock_stays_visible_through_helper_start_finish(self):
        # State: helper start, finish, removal never hide the dock or touch the pause state.
        calls = len(self.face.paused_calls)
        self.start_helper()
        self.app.agents["a1"]["status"] = "Done"
        self.app.agents["a1"]["ended"] = time.time()
        self.cw._paint_panel()
        self.app.agents.clear()
        self.cw._paint_panel()
        self.cw.win.update()
        self.assertTrue(self.cw.dock.visible)
        self.assertTrue(self.cw.dock.frame.winfo_ismapped())
        self.assertEqual(len(self.face.paused_calls), calls, "layout changes must not pause/resume the face")

    def test_render_decision_still_run_after_layout_change(self):
        # Pause logic: a visible, non-iconic, non-paused dock of real size decides 'run'.
        self.start_helper()
        r = self.dock_rect()
        self.assertEqual(head_dock.render_decision(self.cw.dock.frame.winfo_ismapped(), False, False,
                                                   r[2] - r[0], r[3] - r[1]), "run")

    def test_resize_keeps_dock_and_no_overlap(self):
        # Resizing the window with the panel up keeps the dock column and keeps the panel clear of it.
        self.start_helper()
        for g in ("900x700", "1500x900", "1000x800"):
            self.cw.win.geometry(g)
            self.cw.win.update()
            d = self.dock_rect()
            self.assertGreater(d[2] - d[0], 100)
            self.assertFalse(overlap(rect(self.cw.panel_frame), d), g)


if __name__ == "__main__":
    unittest.main()
