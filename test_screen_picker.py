"""Tests for screen_picker: pure logic (no windows) plus one headless Tk smoke test (withdrawn root, no flashing)."""
import unittest

import screen_picker as sp

L = {"index": 0, "name": "DISPLAY1", "rect": [0, 0, 1920, 1080]}
M = {"index": 1, "name": "DISPLAY2", "rect": [1920, 0, 3840, 1080]}
R = {"index": 2, "name": "DISPLAY3", "rect": [3840, 0, 5760, 1080]}


class Rows(unittest.TestCase):
    def test_numbering_sorted_by_left_not_index(self):
        # OBS index order differs from physical order: numbers follow rect.left.
        rows = sp.build_rows([R, L, M])
        self.assertEqual([(r["number"], r["index"]) for r in rows], [(1, 0), (2, 1), (3, 2)])
        rows = sp.build_rows([dict(L, index=5), dict(M, index=3)])
        self.assertEqual([(r["number"], r["index"]) for r in rows], [(1, 5), (2, 3)])

    def test_labels_and_sides(self):
        rows = sp.build_rows([L, M, R])
        self.assertEqual(rows[0]["text"], "Left screen (1920x1080)")
        self.assertEqual(rows[1]["side"], "middle")
        self.assertEqual(rows[2]["text"], "Right screen (1920x1080)")
        self.assertEqual([r["side"] for r in sp.build_rows([L, M])], ["left", "right"])

    def test_single_and_four_screens_have_no_side_words(self):
        self.assertEqual(sp.build_rows([L])[0]["text"], "Screen 1 (1920x1080)")
        four = sp.build_rows([L, M, R, dict(R, index=3, rect=[5760, 0, 7680, 1080])])
        self.assertTrue(all(r["side"] is None for r in four))
        self.assertEqual(four[3]["text"], "Screen 4 (1920x1080)")

    def test_negative_left_monitor_is_screen_1(self):
        rows = sp.build_rows([dict(L, rect=[0, 0, 2560, 1440]), dict(M, index=1, rect=[-1920, 0, 0, 1080])])
        self.assertEqual(rows[0]["index"], 1)

    def test_missing_rects_use_windows_rects(self):
        # label_monitors output has no rect: fall back to the real monitors, matched by position.
        mons = [{"index": 0, "name": "a"}, {"index": 1, "name": "b"}]
        rows = sp.build_rows(mons, [[1920, 0, 3840, 1080], [0, 0, 1920, 1080]])
        self.assertEqual(rows[0]["rect"], [0, 0, 1920, 1080])
        self.assertEqual(rows[0]["size"], "1920x1080")

    def test_no_rects_anywhere_still_numbers_by_index(self):
        rows = sp.build_rows([{"index": 3, "name": "x"}, {"index": 1, "name": "y"}])
        self.assertEqual([r["index"] for r in rows], [1, 3])
        self.assertEqual(rows[0]["text"], "Left screen")

    def test_invalid_input(self):
        self.assertEqual(sp.build_rows(None), [])
        self.assertEqual(sp.build_rows([{"name": "no index"}, "junk"]), [])


class Selection(unittest.TestCase):
    def test_default_is_nothing_ticked_and_record_disabled(self):
        rows = sp.build_rows([L, M])
        sel = sp.default_selection(rows, [100, 100, 500, 500])
        self.assertEqual(sel, set())
        self.assertFalse(sp.record_enabled(sel))

    def test_toggle_and_enable(self):
        sel = sp.toggle(set(), 1)
        self.assertTrue(sp.record_enabled(sel))
        self.assertEqual(sp.toggle(sel, 1), set())      # untick -> disabled again

    def test_chosen_in_screen_order(self):
        rows = sp.build_rows([R, L, M])
        self.assertEqual(sp.chosen_indexes(rows, {2, 0}), [0, 2])
        self.assertEqual(sp.chosen_indexes(rows, set()), [])

    def test_window_screen_number(self):
        rows = sp.build_rows([L, M])
        self.assertEqual(sp.window_screen_number(rows, [2000, 100, 2400, 500]), 2)
        self.assertIsNone(sp.window_screen_number(rows, [9000, 0, 9100, 10]))
        self.assertIsNone(sp.window_screen_number(rows, None))


class Geometry(unittest.TestCase):
    def test_centred_on_monitor(self):
        self.assertEqual(sp.overlay_geometry([1920, 0, 3840, 1080], 360, 360), "360x360+2700+360")

    def test_negative_origin_monitor(self):
        self.assertEqual(sp.overlay_geometry([-1920, 0, 0, 1080], 400, 200), "400x200-1160+440")


DEVS = [{"id": "default", "name": "Default"}, {"id": "{a}", "name": "Headset Mic"}, {"id": "{b}", "name": "Headset Mic"}]


class AudioLogic(unittest.TestCase):
    def test_master_off_mutes_both(self):
        self.assertEqual(sp.effective_audio(False, True, True), (False, False))
        self.assertEqual(sp.effective_audio(True, True, False), (True, False))
        self.assertEqual(sp.effective_audio(True, False, True), (False, True))

    def test_mic_dropdown_only_when_audio_and_mic_ticked(self):
        self.assertTrue(sp.mic_picker_enabled(True, True))
        self.assertFalse(sp.mic_picker_enabled(True, False))
        self.assertFalse(sp.mic_picker_enabled(False, True))

    def test_device_names_unique_and_default_first(self):
        n = sp.device_names(DEVS)
        self.assertEqual(n[0], "Default")
        self.assertEqual(len(set(n)), 3)                       # duplicate display name gets disambiguated
        self.assertEqual(sp.device_names(None), ["Default"])
        self.assertEqual(sp.device_names([{"name": "no id"}, "x"]), ["Default"])

    def test_device_position(self):
        self.assertEqual(sp.device_position(DEVS, "{b}"), 2)
        self.assertEqual(sp.device_position(DEVS, ""), 0)               # blank = Default
        self.assertEqual(sp.device_position(DEVS, "{unplugged}"), 0)    # gone: back to Default
        self.assertEqual(sp.device_position(None, "x"), 0)

    def test_folder_message_passes_warning_and_survives_errors(self):
        self.assertEqual(sp.folder_message("C:/x", lambda p: "careful " + p), "careful C:/x")
        self.assertEqual(sp.folder_message("C:/x", None), "")
        self.assertEqual(sp.folder_message("C:/x", lambda p: 1 / 0), "")
        self.assertEqual(sp.folder_message("C:/x", lambda p: None), "")


class TkOptions(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except Exception:
            self.skipTest("no Tk")
        self.root.withdraw()
        self.got = []

    def tearDown(self):
        self.root.destroy()

    def dlg(self, **prefs):
        opts = {"prefs": prefs, "devices": DEVS,
                "warn": lambda p: "Windows may block OBS writing here" if "Videos" in p else ""}
        d = sp.pick_screens(self.root, [L, M], lambda *a: self.got.append(a), options=opts)
        d.withdraw()
        return d

    def test_opens_with_previous_choice(self):
        d = self.dlg(desktop=False, mic=True, audio=True, record_dir="D:\\Recs", mic_device="{b}")
        self.assertEqual(d.settings(), {"audio": True, "desktop": False, "mic": True, "record_dir": "D:\\Recs",
                                        "mic_device": "{b}"})
        self.assertEqual(d.combo_state(), "readonly")

    def test_defaults_when_nothing_saved(self):
        s = self.dlg().settings()
        self.assertEqual((s["audio"], s["desktop"], s["mic"], s["mic_device"]), (True, True, False, "default"))

    def test_mic_dropdown_follows_mic_tick_and_master(self):
        d = self.dlg(mic=False)
        self.assertEqual(d.combo_state(), "disabled")
        d.toggle_option("mic")
        self.assertEqual(d.combo_state(), "readonly")
        d.toggle_option("audio")                              # master off
        self.assertEqual(d.combo_state(), "disabled")
        d.toggle_option("desktop")                            # greyed: ignored while master is off
        self.assertTrue(d.settings()["desktop"])

    def test_record_returns_settings_and_screens(self):
        d = self.dlg(mic=True)
        d.toggle_index(1)
        d.set_device("{a}")
        d.set_folder("D:\\Out")
        d.record()
        chosen, s = self.got[0]
        self.assertEqual(chosen, [1])
        self.assertEqual((s["mic_device"], s["record_dir"]), ("{a}", "D:\\Out"))

    def test_cancel_returns_none_only(self):
        d = self.dlg()
        d.cancel()
        self.assertEqual(self.got, [(None,)])

    def test_folder_warning_shown_and_cleared(self):
        d = self.dlg(record_dir="C:\\Users\\x\\Videos")
        self.assertIn("Windows may block OBS", d.warning_text())
        d.set_folder("D:\\Safe")
        self.assertEqual(d.warning_text(), "")

    def test_unknown_saved_device_falls_back_to_default(self):
        self.assertEqual(self.dlg(mic_device="{gone}").settings()["mic_device"], "default")

    def test_without_options_old_signature_still_works(self):
        got = []
        d = sp.pick_screens(self.root, [L], got.append)
        d.withdraw()
        d.toggle_index(0)
        d.record()
        self.assertEqual(got, [[0]])


class TkSmoke(unittest.TestCase):
    def test_dialog_logic_headless(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception:
            self.skipTest("no Tk")
        root.withdraw()
        got = []
        try:
            dlg = sp.pick_screens(root, [L, M, R], got.append)
            dlg.withdraw()
            dlg.record()                                 # nothing ticked: ignored
            self.assertEqual(got, [])
            dlg.toggle_index(2)
            dlg.toggle_index(0)
            dlg.record()
            self.assertEqual(got, [[0, 2]])
            dlg2 = sp.pick_screens(root, [L, M], got.append)
            dlg2.withdraw()
            dlg2.cancel()
            self.assertEqual(got[-1], None)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
