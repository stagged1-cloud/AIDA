"""Tests for the single-window mode (status strip + helper panel inside the AIDA chat window).
No real windows: pure functions run directly, ChatWindow methods run on a stub with recording widgets,
and the Overlay methods in voice_claude.py are lifted out of the source with ast (importing the whole
voice app would load speech models)."""
import ast
import pathlib
import time
import types
import unittest

import chat_window as cw

HERE = pathlib.Path(__file__).resolve().parent


def agent(title="Job", status="Working", feed=None, started=None, ended=None):
    return {"title": title, "status": status, "feed": feed if feed is not None else [],
            "started": started if started is not None else time.time() - 65, "ended": ended}


class FakeWidget:
    """Records configure() calls so we can see what was (not) redrawn."""
    def __init__(self):
        self.calls = []
        self.opts = {}

    def configure(self, **kw):
        self.calls.append(kw)
        self.opts.update(kw)

    config = configure

    def set(self, on, animate=True):
        """ToggleSwitch stand-in: remember the last state shown."""
        self.state = bool(on)
        self.calls.append({"set": bool(on)})

    def winfo_children(self):
        return []

    def itemconfig(self, *a, **kw):
        self.calls.append(("item", a, kw))


# ------------------------------------------------------------------ config switch / fallback decision
class TestWanted(unittest.TestCase):
    def test_default_is_single(self):
        # single_window defaults to true when the key is missing
        self.assertTrue(cw.single_window_wanted({}))

    def test_explicit_off(self):
        self.assertFalse(cw.single_window_wanted({"single_window": False}))

    def test_chat_window_disabled_falls_back(self):
        # no chat window = nothing to dock into = old floating boxes
        self.assertFalse(cw.single_window_wanted({"chat_window": False, "single_window": True}))

    def test_chat_module_missing_falls_back(self):
        self.assertFalse(cw.single_window_wanted({"single_window": True}, chat_ok=False))

    def test_none_config(self):
        self.assertTrue(cw.single_window_wanted(None))


# ------------------------------------------------------------------ listening cue / strip
class TestStatusStrip(unittest.TestCase):
    def test_each_state_has_its_colour(self):
        # idle / listening / thinking / speaking / working: dot colour comes from the same palette as the old box
        for state in ("Idle", "Listening", "Thinking", "Speaking", "Working", "Confirm", "Problem"):
            dot, _ = cw.listening_cue(state, "", blink=False)
            self.assertEqual(dot, cw.STATUS_COLOURS[state] if state != "Listening" else "#7A2A2C", state)

    def test_listening_hearing_you_is_bright_with_red_border(self):
        dot, border = cw.listening_cue("Listening", "Hearing you")
        self.assertEqual(dot, cw.STATUS_COLOURS["Listening"])
        self.assertEqual(border, cw.STATUS_COLOURS["Listening"])

    def test_listening_quiet_is_dim_red(self):
        dot, border = cw.listening_cue("Listening", "Press ` to send")
        self.assertEqual((dot, border), ("#7A2A2C", "#3A1D1F"))

    def test_thinking_pulses(self):
        on, _ = cw.listening_cue("Thinking", "", blink=False)
        off, _ = cw.listening_cue("Thinking", "", blink=True)
        self.assertNotEqual(on, off)
        self.assertEqual(off, "#2A2F36")

    def test_speaking_never_pulses_and_border_plain(self):
        self.assertEqual(cw.listening_cue("Speaking", "", True), (cw.STATUS_COLOURS["Speaking"], cw.BG))

    def test_unknown_state_is_idle_colour(self):
        self.assertEqual(cw.listening_cue("Banana")[0], cw.STATUS_COLOURS["Idle"])

    def test_clock_formats(self):
        self.assertEqual(cw.fmt_clock(None), "")
        self.assertEqual(cw.fmt_clock(0), "0:00")
        self.assertEqual(cw.fmt_clock(65.9), "1:05")
        self.assertEqual(cw.fmt_clock(-3), "0:00")          # clock skew must not go negative
        self.assertEqual(cw.fmt_clock(3725), "62:05")

    def test_head_label(self):
        self.assertIn("ON", cw.head_label(True)[0])
        self.assertIn("OFF", cw.head_label(False)[0])
        self.assertNotEqual(cw.head_label(True)[1], cw.head_label(False)[1])


# ------------------------------------------------------------------ helper panel model
class TestPanelModel(unittest.TestCase):
    def test_empty(self):
        m = cw.HelperPanelModel()
        self.assertIsNone(m.sync({}))
        self.assertEqual(m.tabs({}), ([], 0))
        self.assertIsNone(m.view({}, time.time()))

    def test_add_selects_running_first(self):
        # selection prefers a running helper over a finished one
        ag = {"a": agent("A", "Done"), "b": agent("B", "Working")}
        m = cw.HelperPanelModel()
        self.assertEqual(m.sync(ag), "b")

    def test_all_finished_selects_newest(self):
        ag = {"a": agent("A", "Done"), "b": agent("B", "Failed")}
        self.assertEqual(cw.HelperPanelModel().sync(ag), "b")

    def test_selection_sticks_when_more_arrive(self):
        ag = {"a": agent("A")}
        m = cw.HelperPanelModel()
        m.sync(ag)
        ag["b"] = agent("B")
        m.sync(ag)
        self.assertEqual(m.selected, "a")

    def test_selected_removed_reselects(self):
        # a new job clears app.agents: selection must not dangle
        ag = {"a": agent("A"), "b": agent("B")}
        m = cw.HelperPanelModel()
        m.select("a", ag)
        del ag["a"]
        self.assertEqual(m.sync(ag), "b")
        ag.clear()
        self.assertIsNone(m.sync(ag))

    def test_select_unknown_is_refused(self):
        ag = {"a": agent("A")}
        m = cw.HelperPanelModel()
        m.sync(ag)
        self.assertFalse(m.select("zzz", ag))
        self.assertEqual(m.selected, "a")

    def test_select_number(self):
        ag = {"a": agent("A"), "b": agent("B"), "c": agent("C")}
        m = cw.HelperPanelModel()
        self.assertTrue(m.select_number(3, ag))
        self.assertEqual(m.selected, "c")
        self.assertFalse(m.select_number(0, ag))
        self.assertFalse(m.select_number(4, ag))
        self.assertEqual(m.selected, "c")

    def test_finished_and_failed_colouring(self):
        ag = {"a": agent("A", "Done"), "b": agent("B", "Failed"), "c": agent("C", "Working")}
        tabs, _ = cw.HelperPanelModel().tabs(ag)
        cols = {t[1]: t[3] for t in tabs}
        self.assertEqual(cols["a"], cw.STATUS_COLOURS["Done"])
        self.assertEqual(cols["b"], cw.STATUS_COLOURS["Failed"])
        self.assertEqual(cols["c"], cw.STATUS_COLOURS["Working"])

    def test_many_helpers_overflow_keeps_selected_visible(self):
        ag = {f"h{i}": agent(f"Job {i}") for i in range(20)}
        m = cw.HelperPanelModel()
        m.select("h17", ag)
        tabs, more = m.tabs(ag)
        self.assertEqual(len(tabs), cw.HelperPanelModel.MAX_TABS)
        self.assertEqual(more, 20 - cw.HelperPanelModel.MAX_TABS)
        sel = [t for t in tabs if t[4]]
        self.assertEqual(len(sel), 1)
        self.assertEqual(sel[0][1], "h17")
        self.assertEqual(sel[0][0], 18)                      # number is its real position, not its slot

    def test_long_title_clipped(self):
        ag = {"a": agent("x" * 300)}
        m = cw.HelperPanelModel()
        tabs, _ = m.tabs(ag)
        self.assertLessEqual(len(tabs[0][2]), 18)
        v = m.view(ag, time.time())
        self.assertLessEqual(len(v["title"]), 70)

    def test_view_content_and_placeholder(self):
        ag = {"a": agent("A", feed=[])}
        v = cw.HelperPanelModel().view(ag, time.time())
        self.assertEqual(v["lines"], [("Starting", "step")])   # nothing yet: placeholder like the old box
        ag["a"]["feed"] = [(f"line {i}", "step") for i in range(100)]
        v = cw.HelperPanelModel().view(ag, time.time())
        self.assertEqual(len(v["lines"]), cw.HelperPanelModel.MAX_LINES)
        self.assertEqual(v["lines"][-1], ("line 99", "step"))

    def test_clock_stops_at_end(self):
        t0 = 1000.0
        ag = {"a": agent("A", "Done", started=t0, ended=t0 + 125)}
        v = cw.HelperPanelModel().view(ag, t0 + 9999)
        self.assertEqual(v["clock"], "2:05")

    def test_empty_title_ok(self):
        ag = {"a": {"status": "Working", "started": 1.0, "ended": None}}     # no title, no feed key at all
        m = cw.HelperPanelModel()
        self.assertEqual(m.tabs(ag)[0][0][2], "")
        self.assertEqual(m.view(ag, 5.0)["lines"], [("Starting", "step")])


# ------------------------------------------------------------------ ChatWindow methods on a stub (no Tk)
def stub_window(state=None, agents=None, overlay=None, single=True):
    w = object.__new__(cw.ChatWindow)
    w.single = single
    w.state = state if state is not None else {}
    w.saved = []
    w.save_state = lambda st: w.saved.append(dict(st))
    w.app = types.SimpleNamespace(agents=agents if agents is not None else {}, overlay=overlay)
    w.win = FakeWidget()
    w.win.attributes = lambda *a: w.win.calls.append(("attr", a))
    w.pin_lbl, w.head_lbl, w.clock_lbl = FakeWidget(), FakeWidget(), FakeWidget()
    w.dot, w.dot_id = FakeWidget(), 1
    w.state_lbl, w.detail_lbl, w.helpers = FakeWidget(), FakeWidget(), FakeWidget()
    w.panel = cw.HelperPanelModel(bool(w.state.get("chat_helpers_collapsed", False)))
    w.pinned = bool(w.state.get("chat_pin", False))
    w._panel_view = w._tabs_view = None
    w._clock = None
    w._blink_n = 0
    w.s = 1.0
    return w


class TestWindowBehaviour(unittest.TestCase):
    def test_pin_toggle_persists_and_applies(self):
        w = stub_window()
        w.toggle_pin()
        self.assertTrue(w.pinned)
        self.assertTrue(w.state["chat_pin"])
        self.assertEqual(w.saved[-1]["chat_pin"], True)
        self.assertIn(("attr", ("-topmost", True)), w.win.calls)
        w.toggle_pin()
        self.assertFalse(w.state["chat_pin"])
        self.assertIn(("attr", ("-topmost", False)), w.win.calls)

    def test_pin_label_text_differs(self):
        w = stub_window()
        w._paint_pin()
        off = w.pin_lbl.state
        w.toggle_pin()
        self.assertNotEqual(w.pin_lbl.state, off)

    def test_head_toggle_goes_to_controller(self):
        calls = []
        head = types.SimpleNamespace(is_on=True, toggle_later=lambda: calls.append("t"))
        w = stub_window(overlay=types.SimpleNamespace(head=head))
        w.toggle_head()
        self.assertEqual(calls, ["t"])
        self.assertTrue(w._head_is_on())

    def test_head_toggle_without_overlay_is_harmless(self):
        w = stub_window(overlay=None)
        w.toggle_head()
        self.assertFalse(w._head_is_on())

    def test_head_changed_repaints(self):
        w = stub_window()
        w.head_changed(True)
        self.assertTrue(w.head_lbl.state)
        w.head_changed(False)
        self.assertFalse(w.head_lbl.state)

    def test_head_changed_ignored_in_legacy_mode(self):
        w = stub_window(single=False)
        w.head_changed(True)                                   # no head_lbl in legacy mode: must not touch it
        self.assertEqual(w.head_lbl.calls, [])

    def test_collapse_persists(self):
        w = stub_window(agents={"a": agent()})
        w._paint_panel = lambda: None
        w.toggle_panel()
        self.assertTrue(w.state["chat_helpers_collapsed"])
        self.assertTrue(w.panel.collapsed)
        w.toggle_panel()
        self.assertFalse(w.state["chat_helpers_collapsed"])

    def test_collapsed_state_restored_from_saved_state(self):
        w = stub_window(state={"chat_helpers_collapsed": True, "chat_pin": True})
        self.assertTrue(w.panel.collapsed)
        self.assertTrue(w.pinned)

    def test_selecting_tab_expands_collapsed_panel(self):
        ag = {"a": agent("A"), "b": agent("B")}
        w = stub_window(state={"chat_helpers_collapsed": True}, agents=ag)
        w._paint_panel = lambda: None
        w.select_helper("b")
        self.assertEqual(w.panel.selected, "b")
        self.assertFalse(w.panel.collapsed)
        self.assertFalse(w.state["chat_helpers_collapsed"])

    def test_quit_still_confirms(self):
        quits = []
        w = stub_window()
        w.app.quit_app = lambda why: quits.append(why)
        self.assertFalse(w.confirm_quit(ask=lambda: False))
        self.assertEqual(quits, [])
        self.assertTrue(w.confirm_quit(ask=lambda: True))
        self.assertEqual(quits, ["Quit button"])

    def test_status_paint_has_no_duplicate_updates(self):
        # same state twice in a row: the dot, border and clock are only touched once
        w = stub_window()
        w.app.status = lambda: ("Speaking", "Talking", 12.0)
        w._paint_panel = lambda: None
        w._view = None
        w._paint_status()
        n_dot, n_win, n_clock = len(w.dot.calls), len(w.win.calls), len(w.clock_lbl.calls)
        w._paint_status()
        self.assertEqual((len(w.dot.calls), len(w.win.calls), len(w.clock_lbl.calls)), (n_dot, n_win, n_clock))
        self.assertEqual(w.state_lbl.opts["text"], "Speaking")

    def test_status_paint_follows_state_changes(self):
        w = stub_window()
        status = {"v": ("Listening", "Hearing you", 1.0)}
        w.app.status = lambda: status["v"]
        w._paint_panel = lambda: None
        w._view = None
        w._paint_status()
        self.assertEqual(w.win.opts["bg"], cw.STATUS_COLOURS["Listening"])
        status["v"] = ("Confirm", "Say confirmed", 2.0)
        w._paint_status()
        self.assertEqual(w.win.opts["bg"], cw.BG)
        self.assertEqual(w.state_lbl.opts["text"], "Waiting for you")


# ------------------------------------------------------------------ Overlay side (voice_claude.py source)
def overlay_methods():
    src = (HERE / "voice_claude.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Overlay")
    wanted = {"go_single", "go_legacy", "_head_changed"}
    fns = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    mod = ast.Module(body=[ast.ClassDef(name="O", bases=[], keywords=[], body=fns, decorator_list=[])],
                     type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"Pane": types.SimpleNamespace(DIM="#6F7883")}
    exec(compile(mod, "overlay_methods", "exec"), ns)
    return ns["O"], src


class StubOverlay:
    def __init__(self, cls):
        self.cls = cls
        self.root = types.SimpleNamespace(calls=[], withdraw=lambda: self.root.calls.append("withdraw"),
                                          deiconify=lambda: self.root.calls.append("deiconify"))
        self.main = types.SimpleNamespace(no_focus=lambda: self.root.calls.append("no_focus"))
        self.destroyed = []
        self.panes = {"a": types.SimpleNamespace(destroy=lambda: self.destroyed.append("a")),
                      "b": types.SimpleNamespace(destroy=lambda: self.destroyed.append("b"))}
        self.head_btn = FakeWidget()
        self.chat = None
        self.app = types.SimpleNamespace(chat=None)
        self.single, self.want_single = False, True
        self.head_starts = 0
        self._start_head = lambda: setattr(self, "head_starts", self.head_starts + 1)   # head start-up is deferred
        for n in ("go_single", "go_legacy", "_head_changed"):
            setattr(self, n, getattr(cls, n).__get__(self))


class TestOverlayFallback(unittest.TestCase):
    def setUp(self):
        self.cls, self.src = overlay_methods()

    def test_go_single_hides_root_and_destroys_agent_boxes(self):
        o = StubOverlay(self.cls)
        o.go_single()
        self.assertTrue(o.single)
        self.assertIn("withdraw", o.root.calls)
        self.assertEqual(sorted(o.destroyed), ["a", "b"])
        self.assertEqual(o.panes, {})
        self.assertEqual(o.head_starts, 1)                      # docked head can only start once the window exists

    def test_go_legacy_shows_the_old_box(self):
        # chat window failed to build: the old Overlay appears exactly as before
        o = StubOverlay(self.cls)
        o.go_legacy()
        self.assertFalse(o.single)
        self.assertFalse(o.want_single)
        self.assertIn("deiconify", o.root.calls)
        self.assertNotIn("withdraw", o.root.calls)

    def test_head_changed_updates_old_label_and_chat(self):
        o = StubOverlay(self.cls)
        seen = []
        o.app.chat = types.SimpleNamespace(single=True, head_changed=seen.append)
        o._head_changed(True)
        self.assertIn("HEAD ON", o.head_btn.opts["text"])
        self.assertEqual(seen, [True])

    def test_head_changed_without_chat_does_not_fail(self):
        o = StubOverlay(self.cls)
        o._head_changed(False)
        self.assertIn("HEAD OFF", o.head_btn.opts["text"])

    def test_head_changed_chat_error_is_swallowed(self):
        o = StubOverlay(self.cls)

        def boom(on):
            raise RuntimeError("x")
        o.app.chat = types.SimpleNamespace(single=True, head_changed=boom)
        o._head_changed(True)                                   # must not raise (runs on the Tk thread)

    def test_wiring_falls_back_on_chat_failure(self):
        # source-level check of App: single mode only after the chat window built; legacy on any failure
        i = self.src.index("single = self.overlay.want_single")
        block = self.src[i - 200:i + 900]
        self.assertIn("single=single", block)
        self.assertIn("self.overlay.go_single()", block)
        self.assertIn("self.overlay.go_legacy()", block)
        self.assertLess(block.index("ChatWindow("), block.index("go_single()"))   # window first, then drop the boxes

    def test_root_not_shown_when_single_wanted(self):
        # the old box must not flash on start-up in single mode
        i = self.src.index("self.want_single = bool(")
        self.assertIn("if not self.want_single:", self.src[i:i + 400])

    def test_tick_skips_floating_boxes_in_single_mode(self):
        i = self.src.index("if self.single or self.want_single:")
        self.assertIn("return", self.src[i:i + 1500])


if __name__ == "__main__":
    unittest.main()
