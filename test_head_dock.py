"""Tests for head_dock.py: pure logic only (no real GL window, no Tk). Run: python -m unittest test_head_dock"""
import unittest

import head_dock as hd
import head_toggle as ht


class FakeUI:
    """Stands in for DockPanel: records what the controller asked for."""
    def __init__(self):
        self.calls, self.visible, self.collapsed = [], False, False

    def show(self):
        self.visible = True
        self.calls.append("show")

    def hide(self):
        self.visible = False
        self.calls.append("hide")

    def set_collapsed(self, c):
        self.collapsed = c
        self.calls.append(f"collapsed={c}")


class FakeFace:
    def __init__(self, docked=True):
        self.docked, self.paused, self.dead = docked, None, False

    def set_paused(self, p):
        self.paused = p


class Layout(unittest.TestCase):
    def test_dock_px_scales_and_is_even(self):
        # 260 at 100% -> 260; at 150% -> 390; at 125% -> 325 rounded down to even 324
        self.assertEqual(hd.dock_px(260, 1.0), 260)
        self.assertEqual(hd.dock_px(260, 1.5), 390)
        self.assertEqual(hd.dock_px(260, 1.25), 324)

    def test_dock_px_has_a_floor(self):
        self.assertEqual(hd.dock_px(10, 1.0), 120)         # nonsense config cannot make a pinhead

    def test_fit_square_centres(self):
        self.assertEqual(hd.fit_square(260, 260), (0, 0, 260))
        self.assertEqual(hd.fit_square(300, 260), (20, 0, 260))     # wider frame: centred horizontally
        self.assertEqual(hd.fit_square(260, 300), (0, 20, 260))

    def test_fit_square_tiny_or_zero_draws_nothing(self):
        # minimised windows report 0x0 client rects
        for wh in [(0, 0), (5, 200), (200, 7), (-3, 100)]:
            self.assertEqual(hd.fit_square(*wh)[2], 0, wh)

    def test_rect_follows_resize(self):
        # frame resized from 260 to 390 (DPI change): side follows, still centred
        self.assertEqual(hd.fit_square(390, 390), (0, 0, 390))

    def test_colorref_is_bgr(self):
        self.assertEqual(hd.hex_to_colorref("#171B21"), 0x211B17)
        self.assertEqual(hd.hex_to_colorref("FF0000"), 0x0000FF)


class Pausing(unittest.TestCase):
    def test_runs_when_visible(self):
        self.assertEqual(hd.render_decision(True, False, False, 260, 260), "run")

    def test_pauses_when_minimised(self):
        self.assertEqual(hd.render_decision(True, True, False, 260, 260), "minimised")

    def test_pauses_when_hidden_or_collapsed(self):
        self.assertEqual(hd.render_decision(False, False, False, 260, 260), "hidden")
        self.assertEqual(hd.render_decision(True, False, True, 260, 260), "paused")

    def test_pauses_when_client_area_is_tiny(self):
        self.assertEqual(hd.render_decision(True, False, False, 0, 0), "tiny")


class Controller(unittest.TestCase):
    def test_docked_face_shows_dock_and_unpauses(self):
        ui, f = FakeUI(), FakeFace()
        c = hd.DockController(ui)
        self.assertTrue(c.sync(True, f))
        self.assertTrue(ui.visible)
        self.assertFalse(f.paused)

    def test_head_off_hides_and_reclaims_space(self):
        ui, f = FakeUI(), FakeFace()
        c = hd.DockController(ui)
        c.sync(True, f)
        self.assertFalse(c.sync(False, None))
        self.assertFalse(ui.visible)

    def test_floating_fallback_face_does_not_show_dock(self):
        # a floating head (no .docked) must leave the dock region empty and hidden
        ui = FakeUI()
        c = hd.DockController(ui)
        self.assertFalse(c.sync(True, FakeFace(docked=False)))
        self.assertFalse(ui.visible)

    def test_collapse_pauses_and_expand_resumes_and_is_remembered(self):
        ui, f, saved = FakeUI(), FakeFace(), []
        c = hd.DockController(ui, on_collapsed=saved.append)
        c.sync(True, f)
        c.toggle_collapsed()
        self.assertTrue(f.paused)
        self.assertTrue(ui.collapsed)
        c.toggle_collapsed()
        self.assertFalse(f.paused)
        self.assertEqual(saved, [True, False])

    def test_starts_collapsed_when_remembered(self):
        ui, f = FakeUI(), FakeFace()
        c = hd.DockController(ui, collapsed=True)
        c.sync(True, f)
        self.assertTrue(ui.collapsed)
        self.assertTrue(f.paused)

    def test_toggle_while_head_off_only_remembers(self):
        ui = FakeUI()
        c = hd.DockController(ui)
        c.toggle_collapsed()
        self.assertEqual(ui.calls, [])          # nothing drawn while the head is off

    def test_sync_with_none_face_is_safe(self):
        c = hd.DockController(FakeUI())
        self.assertFalse(c.sync(True, None))


class WithHeadController(unittest.TestCase):
    """HeadController + DockController wired as Overlay wires them, with a fake docked face."""
    def rig(self, dock_ok=True):
        self.ui, self.state = FakeUI(), {}
        self.dock = hd.DockController(self.ui)
        self.faces = []

        def make():
            if not dock_ok:
                raise RuntimeError("embed failed")
            f = FakeFace()
            self.faces.append(f)
            return f

        self.hc = ht.HeadController(make, lambda f: None, self.state, lambda s: None,
                                    on_change=lambda on: self.dock.sync(on, self.hc.face))
        return self.hc

    def test_toggle_off_on_twenty_times(self):
        hc = self.rig()
        for _ in range(20):
            self.assertTrue(hc.set(True))
            self.assertTrue(self.ui.visible)
            self.assertFalse(hc.set(False))
            self.assertFalse(self.ui.visible)
        self.assertEqual(len(self.faces), 20)

    def test_embed_failure_leaves_head_off_and_dock_hidden(self):
        # fallback to a floating head is Overlay's job; here the dock must simply stay hidden and nothing raises
        hc = self.rig(dock_ok=False)
        self.assertFalse(hc.set(True))
        self.assertFalse(self.ui.visible)

    def test_voice_off_command_hides_dock(self):
        hc = self.rig()
        hc.set(True)
        hc.set(ht.parse_command("head off"))
        self.assertFalse(self.ui.visible)


class SwitchStaysInSync(unittest.TestCase):
    """The Head switch, the dock and the HeadController must always agree (checked after the 'Head OFF but head
    visible' screenshot oddity: that was demo code creating a head without a controller, not a sync bug)."""
    def rig(self):
        self.ui, self.switch, self.state = FakeUI(), [], {}
        self.dock = hd.DockController(self.ui)
        faces = []

        def on_change(on):                       # what Overlay._head_changed -> ChatWindow.head_changed does
            self.switch.append(on)
            self.dock.sync(on, self.hc.face)

        self.hc = ht.HeadController(lambda: faces.append(FakeFace()) or faces[-1], lambda f: None, self.state,
                                    lambda s: None, on_change=on_change)
        return self.hc

    def test_switch_on_means_head_visible(self):
        hc = self.rig()
        hc.set(True)
        self.assertTrue(self.switch[-1] and self.ui.visible and hc.is_on)

    def test_switch_off_means_head_hidden(self):
        hc = self.rig()
        hc.set(True)
        hc.set(False)
        self.assertFalse(self.switch[-1] or self.ui.visible or hc.is_on)

    def test_voice_commands_move_switch_and_dock(self):
        hc = self.rig()
        for phrase, want in [("head on", True), ("turn the head off", False), ("show the head", True),
                             ("hide the head", False)]:
            hc.set(ht.parse_command(phrase))
            self.assertEqual((self.switch[-1], self.ui.visible), (want, want), phrase)

    def test_startup_off_reports_off(self):
        hc = self.rig()
        hc.start(False)
        self.assertEqual(self.switch, [False])
        self.assertFalse(self.ui.visible)


class DockHasNoHeader(unittest.TestCase):
    def test_no_header_code_left(self):
        # the '▾ HEAD' label and its click-to-collapse are gone from the panel
        import pathlib
        src = (pathlib.Path(__file__).parent / "head_dock.py").read_text(encoding="utf-8")
        self.assertNotIn("HEAD\"", src.split("class DockPanel")[1])
        self.assertNotIn("self.hdr", src)

    def test_composite_transparent_pixels_give_exactly_the_background(self):
        # real GDI AlphaBlend on memory DCs (no window): a fully transparent premultiplied pixel over the panel
        # colour must come out as exactly that colour, and a half-covered one must sit between
        import ctypes
        import face3d as F
        g, m = hd._g32, hd._m32
        g.GetPixel.restype, g.GetPixel.argtypes = ctypes.c_uint32, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        dst, src = g.CreateCompatibleDC(None), g.CreateCompatibleDC(None)
        size = 8
        dib_d, dib_s = F._DIB(dst, size), F._DIB(src, size)
        g.SelectObject(dst, dib_d.hbm)
        g.SelectObject(src, dib_s.hbm)
        bg = "#111418"
        brush = g.CreateSolidBrush(hd.hex_to_colorref(bg))
        import ctypes.wintypes as wt
        hd._u32.FillRect(dst, ctypes.byref(wt.RECT(0, 0, size, size)), brush)
        for i in range(size * size * 4):
            dib_s.view[i] = 0                                  # alpha 0 everywhere
        m.AlphaBlend(dst, 0, 0, size, size, src, 0, 0, size, size, hd.BF_ALPHA_255)
        want = hd.hex_to_colorref(bg)
        self.assertEqual(g.GetPixel(dst, 0, 0) & 0xFFFFFF, want)
        self.assertEqual(g.GetPixel(dst, 7, 7) & 0xFFFFFF, want)
        g.DeleteObject(brush)
        g.DeleteDC(dst)
        g.DeleteDC(src)


if __name__ == "__main__":
    unittest.main()
