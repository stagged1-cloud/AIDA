"""Tests for hotkeys.py (key parsing, validation, live rebind, persistence) and the hotkey UI wiring.
No windows, no global hooks: the backend is a fake. Run: .venv\\Scripts\\python -m unittest test_hotkey_menu -v"""
import pathlib
import unittest

import hotkeys as hk

HERE = pathlib.Path(__file__).parent


class FakeBackend:
    """Records add/remove calls like the `keyboard` package would; can be told to refuse specific keys."""
    def __init__(self, refuse=()):
        self.calls, self.live, self.refuse, self.n = [], {}, set(refuse), 0

    def add(self, spec, cb):
        self.calls.append(("add", spec))
        if spec in self.refuse:
            raise ValueError(f"cannot hook {spec}")
        self.n += 1
        self.live[self.n] = (spec, cb)
        return self.n

    def remove(self, h):
        self.calls.append(("remove", self.live[h][0]))
        del self.live[h]

    def specs(self):
        return sorted(s for s, _ in self.live.values())


def mgr(refuse=(), ptt="`", cancel="ctrl+`"):
    be = FakeBackend(refuse)
    return be, hk.HotkeyManager(be, {"ptt": lambda: "p", "cancel": lambda: "c"}, {"ptt": ptt, "cancel": cancel})


class Normalising(unittest.TestCase):
    def test_single_keys(self):
        # a plain key and a function key come out lower-case
        self.assertEqual(hk.normalise("F9"), "f9")
        self.assertEqual(hk.normalise("`"), "`")

    def test_combo_order_is_canonical(self):
        # modifiers always ctrl, alt, shift, windows then key, whatever order they were typed in
        self.assertEqual(hk.normalise("alt+ctrl+t"), "ctrl+alt+t")
        self.assertEqual(hk.normalise("shift+ctrl+f5"), "ctrl+shift+f5")

    def test_aliases(self):
        # control/escape/grave/pgup etc. map to the names the keyboard package uses
        self.assertEqual(hk.normalise("Control+Grave"), "ctrl+`")
        self.assertEqual(hk.normalise("ctrl+backtick"), "ctrl+`")
        self.assertEqual(hk.normalise("Escape"), "esc")
        self.assertEqual(hk.normalise("PgUp"), "page up")
        self.assertEqual(hk.normalise("win+x"), "windows+x")

    def test_empty_is_empty(self):
        self.assertEqual(hk.normalise(""), "")
        self.assertEqual(hk.normalise(None), "")

    def test_invalid_inputs_raise(self):
        # modifiers only, two keys, nonsense names and repeated modifiers are all refused
        for bad in ("ctrl", "ctrl+alt", "a+b", "banana", "ctrl+ctrl+a", "ctrl+f99"):
            with self.assertRaises(ValueError, msg=bad):
                hk.normalise(bad)

    def test_display(self):
        self.assertEqual(hk.display("ctrl+alt+t"), "Ctrl+Alt+T")
        self.assertEqual(hk.display("f9"), "F9")
        self.assertEqual(hk.display("`"), "`")
        self.assertEqual(hk.display("ctrl+`"), "Ctrl+`")
        self.assertEqual(hk.display("page up"), "Page Up")
        self.assertEqual(hk.display(""), "not set")


class TkEvents(unittest.TestCase):
    def test_function_key(self):
        self.assertEqual(hk.event_to_spec("F9", 0), ("f9", ""))

    def test_ctrl_space(self):
        self.assertEqual(hk.event_to_spec("space", hk.ST_CTRL)[0], "ctrl+space")

    def test_ctrl_alt_t_uses_letter_not_case(self):
        # shift/caps can't turn the key into 'T'
        self.assertEqual(hk.event_to_spec("T", hk.ST_CTRL | hk.ST_ALT)[0], "ctrl+alt+t")

    def test_backtick_on_uk_layout(self):
        # Tk calls the key 'grave'; with Ctrl it is the cancel default
        self.assertEqual(hk.event_to_spec("grave", hk.ST_CTRL)[0], "ctrl+`")

    def test_bare_modifier_keeps_waiting(self):
        self.assertEqual(hk.event_to_spec("Control_L", hk.ST_CTRL), (None, ""))
        self.assertEqual(hk.event_to_spec("Shift_L", hk.ST_SHIFT), (None, ""))

    def test_unknown_shifted_symbol_gives_a_hint(self):
        spec, note = hk.event_to_spec("notsign", hk.ST_SHIFT)
        self.assertIsNone(spec)
        self.assertIn("without Shift", note)

    def test_win_key_flag_adds_windows(self):
        self.assertEqual(hk.event_to_spec("x", 0, win_down=True)[0], "windows+x")


class Validation(unittest.TestCase):
    def v(self, spec, which="ptt", other="ctrl+`"):
        return hk.validate(spec, which, other)

    def test_function_key_ok(self):
        self.assertEqual(self.v("f9").level, "ok")

    def test_ctrl_combo_ok(self):
        self.assertEqual(self.v("ctrl+space").level, "ok")

    def test_plain_letter_digit_space_refused(self):
        # a global hotkey on a typing key would fire while typing in the text box: no override allowed
        for k in ("a", "5", "space", "shift+a", ";"):
            self.assertEqual(self.v(k).level, "error", k)

    def test_editing_keys_warn_with_override(self):
        for k in ("enter", "backspace", "left"):
            self.assertEqual(self.v(k).level, "warn", k)

    def test_ctrl_alt_letter_warns_altgr(self):
        self.assertEqual(self.v("ctrl+alt+t").level, "warn")

    def test_windows_combos_refused(self):
        self.assertEqual(self.v("windows+x").level, "error")

    def test_reserved_refused(self):
        for k in ("ctrl+alt+delete", "alt+f4", "alt+tab", "ctrl+c", "ctrl+v", "ctrl+shift+esc"):
            self.assertEqual(self.v(k).level, "error", k)

    def test_duplicate_of_other_key_refused(self):
        self.assertEqual(self.v("ctrl+`", "ptt", "ctrl+`").level, "error")
        self.assertEqual(self.v("f9", "cancel", "f9").level, "error")
        self.assertEqual(self.v("Control+Grave", "ptt", "ctrl+`").level, "error")   # alias spelling still caught

    def test_defaults_always_accepted(self):
        # the shipped push-to-talk key is a plain backtick, so Reset / default must pass
        self.assertEqual(self.v("`", "ptt", "ctrl+`").level, "ok")

    def test_esc_alone_ok_for_cancel_warn_for_ptt(self):
        self.assertEqual(self.v("esc", "cancel", "f9").level, "ok")
        self.assertEqual(self.v("esc", "ptt", "ctrl+`").level, "warn")

    def test_empty_ptt_refused_empty_cancel_ok(self):
        self.assertEqual(self.v("", "ptt").level, "error")
        self.assertEqual(self.v("", "cancel", "f9").level, "ok")

    def test_garbage_is_error_not_exception(self):
        self.assertEqual(self.v("ctrl+").level, "error")
        self.assertEqual(self.v("blah blah").level, "error")


class CaptureFlow(unittest.TestCase):
    def test_warn_needs_use_anyway(self):
        m = hk.CaptureModel("ptt", "`", "ctrl+`")
        m.key("Return", 0)
        self.assertFalse(m.can_save)
        self.assertTrue(m.needs_override)
        m.use_anyway()
        self.assertTrue(m.can_save)
        self.assertEqual(m.result(), "enter")

    def test_error_cannot_be_overridden(self):
        m = hk.CaptureModel("ptt", "`", "ctrl+`")
        m.key("a", 0)
        m.use_anyway()
        self.assertFalse(m.can_save)
        self.assertIsNone(m.result())

    def test_new_key_resets_override(self):
        m = hk.CaptureModel("ptt", "`", "ctrl+`")
        m.key("Return", 0)
        m.use_anyway()
        m.key("F8", 0)
        self.assertFalse(m.override)
        self.assertEqual(m.result(), "f8")

    def test_win_key_rejected(self):
        m = hk.CaptureModel("ptt", "`", "ctrl+`")
        m.key("x", 0, win_down=True)
        self.assertEqual(m.verdict.level, "error")

    def test_clear_cancel_allowed_clear_ptt_not(self):
        c = hk.CaptureModel("cancel", "ctrl+`", "f9")
        c.clear()
        self.assertTrue(c.can_save)
        self.assertEqual(c.result(), "")
        p = hk.CaptureModel("ptt", "f9", "ctrl+`")
        p.clear()
        self.assertFalse(p.can_save)

    def test_nothing_pressed_cannot_save(self):
        self.assertFalse(hk.CaptureModel("ptt", "f9", "").can_save)


class LiveRebind(unittest.TestCase):
    def test_initial_registration(self):
        be, m = mgr()
        self.assertEqual(be.specs(), ["`", "ctrl+`"])

    def test_rebind_unregisters_old_then_registers_new(self):
        be, m = mgr()
        be.calls.clear()
        self.assertEqual(m.rebind("ptt", "F9"), "f9")
        self.assertEqual(be.calls, [("remove", "`"), ("add", "f9")])      # order matters
        self.assertEqual(be.specs(), ["ctrl+`", "f9"])
        self.assertEqual(m.bound["ptt"], "f9")

    def test_new_callback_is_the_original_handler(self):
        be, m = mgr()
        m.rebind("ptt", "f9")
        cb = [c for s, c in be.live.values() if s == "f9"][0]
        self.assertEqual(cb(), "p")                  # still toggles push-to-talk, not cancel

    def test_failure_rolls_back_to_old_key(self):
        be, m = mgr(refuse={"f9"})
        with self.assertRaises(RuntimeError) as cm:
            m.rebind("ptt", "f9")
        self.assertIn("Kept", str(cm.exception))
        self.assertEqual(m.bound["ptt"], "`")
        self.assertEqual(be.specs(), ["`", "ctrl+`"])

    def test_failure_and_failed_restore_reports_both(self):
        be, m = mgr()
        be.refuse = {"f9", "`"}
        with self.assertRaises(RuntimeError) as cm:
            m.rebind("ptt", "f9")
        self.assertIn("could not restore", str(cm.exception))
        self.assertEqual(m.bound["ptt"], "")

    def test_clear_cancel_unregisters_only(self):
        be, m = mgr()
        m.rebind("cancel", "")
        self.assertEqual(be.specs(), ["`"])
        self.assertEqual(m.bound["cancel"], "")

    def test_other_binding_lookup(self):
        _, m = mgr()
        self.assertEqual(m.other("ptt"), "ctrl+`")
        self.assertEqual(m.other("cancel"), "`")

    def test_swap_via_free_then_take(self):
        # the order reset_hotkeys uses: free cancel, move ptt, then re-add cancel: never two owners of one key
        be, m = mgr(ptt="f9", cancel="ctrl+`")
        m.rebind("cancel", "")
        m.rebind("ptt", "`")
        m.rebind("cancel", "ctrl+`")
        self.assertEqual(be.specs(), ["`", "ctrl+`"])


class Persistence(unittest.TestCase):
    CFG = {"hotkey": "`", "cancel_hotkey": "ctrl+`"}

    def test_config_when_nothing_saved(self):
        self.assertEqual(hk.resolve({}, self.CFG), {"ptt": "`", "cancel": "ctrl+`"})

    def test_saved_choice_wins_over_config(self):
        r = hk.resolve({"hotkey": "F9"}, self.CFG)
        self.assertEqual(r["ptt"], "f9")
        self.assertEqual(r["cancel"], "ctrl+`")

    def test_saved_empty_cancel_means_off(self):
        self.assertEqual(hk.resolve({"cancel_hotkey": ""}, self.CFG)["cancel"], "")

    def test_saved_empty_ptt_falls_back(self):
        self.assertEqual(hk.resolve({"hotkey": ""}, self.CFG)["ptt"], "`")

    def test_garbled_saved_value_ignored(self):
        self.assertEqual(hk.resolve({"hotkey": "ctrl+alt"}, self.CFG)["ptt"], "`")

    def test_builtin_defaults_when_config_missing(self):
        self.assertEqual(hk.resolve({}, {}), {"ptt": "`", "cancel": "ctrl+`"})

    def test_config_defaults_unchanged_in_repo(self):
        # config.yaml still ships the original keys
        t = (HERE / "config.example.yaml").read_text(encoding="utf-8")
        self.assertIn('hotkey: "`"', t)
        self.assertIn('cancel_hotkey: "ctrl+`"', t)


class AppWiring(unittest.TestCase):
    def test_source_wiring(self):
        # status text follows the live binding; startup registers via the manager with the saved-wins resolver
        src = (HERE / "voice_claude.py").read_text(encoding="utf-8")
        self.assertIn("Press {hotkeys.display(self.hk('ptt'))} to talk", src)
        self.assertIn("again to send", src)
        self.assertIn("hotkeys.resolve(self.state, CFG)", src)
        self.assertIn("hotkeys.HotkeyManager(", src)
        self.assertNotIn("keyboard.add_hotkey(hot", src)

    def test_ui_text_updates_after_rebind(self):
        # the menu labels are built from app.hotkey_bindings(), so a rebind shows up immediately
        import hotkey_ui

        class A:
            keys = {"ptt": "`", "cancel": "ctrl+`"}
            def hotkey_bindings(self): return dict(self.keys)
            def set_hotkey(self, n, s): self.keys[n] = hk.normalise(s)

        a = A()
        strip = hotkey_ui.MenuStrip.__new__(hotkey_ui.MenuStrip)
        strip.app = a
        self.assertIn("[`]", strip.labels()[0])
        a.set_hotkey("ptt", "F9")
        self.assertIn("[F9]", strip.labels()[0])
        self.assertIn("[Ctrl+`]", strip.labels()[1])

    def test_menu_needs_app_support(self):
        import hotkey_ui
        self.assertFalse(hotkey_ui.app_supports(object()))
        self.assertIsNone(hotkey_ui.build_menubar(None, None, {}, 1.0, object(), print))


class FakeFrame:
    """Records constructor kwargs so colours can be compared without creating a real window."""
    made = []

    def __init__(self, parent=None, **kw):
        self.kw = kw
        FakeFrame.made.append(self)

    def pack_propagate(self, *a): pass
    def pack(self, **kw): self.packed = kw
    def pack_forget(self): pass
    def winfo_manager(self): return "pack"
    def update_idletasks(self): pass
    def configure(self, **kw): self.kw.update(kw)


class FakeTkModule:
    Frame = FakeFrame


class DockHasNoBox(unittest.TestCase):
    def test_dock_widgets_all_use_window_background(self):
        # no panel/border colour: every dock widget is exactly the window background, no border or highlight
        import head_dock
        FakeFrame.made = []
        BG = "#111418"
        p = head_dock.DockPanel(FakeTkModule, object(), object(),
                                (BG, "#171B21", "#2A2F36", "#6F7883", "#AAB3BE"), 1.0, 260)
        self.assertEqual(len(FakeFrame.made), 3)
        for f in FakeFrame.made:
            self.assertEqual(f.kw["bg"], BG)
            self.assertEqual(f.kw.get("highlightthickness", 0), 0)
            self.assertEqual(f.kw.get("bd", 0), 0)
        self.assertEqual(p.PANEL, BG)               # what DockedHead blends over
        self.assertFalse(hasattr(p, "hdr"))         # the header is gone
        self.assertEqual(p.box.kw["width"], 260)    # box is exactly the head size: no edge frame


if __name__ == "__main__":
    unittest.main()
