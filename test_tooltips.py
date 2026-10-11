"""Tooltip regression tests (stuck 'Read typed replies aloud' tip, 10 Oct 2026). Fake widgets and a manual clock:
no real Tk window is created."""
import unittest
import chat_window as cw


class FakeEvent:
    def __init__(self, widget=None, x=100, y=100):
        self.widget, self.x_root, self.y_root = widget, x, y


class FakeTop:
    def __init__(self):
        self.b = {}

    def bind(self, ev, fn, add=None):
        self.b.setdefault(ev, []).append(fn)

    def fire(self, ev):
        for fn in self.b.get(ev, []):
            fn(FakeEvent(self))


class Clock:
    def __init__(self):
        self.t, self.jobs, self.n = 0, {}, 0

    def advance(self, ms):
        end = self.t + ms
        while True:
            due = sorted((t, j) for j, (t, f) in self.jobs.items() if t <= end)
            if not due:
                break
            t, j = due[0]
            self.t = t
            f = self.jobs.pop(j)[1]
            f()
        self.t = end


class FakeWidget:
    def __init__(self, clock, top, inside=True):
        self.clock, self.top, self.inside = clock, top, inside
        self.b = {}

    def bind(self, ev, fn, add=None):
        self.b.setdefault(ev, []).append(fn)

    def fire(self, ev):
        for fn in self.b.get(ev, []):
            fn(FakeEvent(self))

    def after(self, ms, fn):
        self.clock.n += 1
        self.clock.jobs[self.clock.n] = (self.clock.t + ms, fn)
        return self.clock.n

    def after_cancel(self, j):
        self.clock.jobs.pop(j, None)

    def winfo_toplevel(self):
        return self.top

    def winfo_pointerxy(self):
        return (50, 20) if self.inside else (900, 900)

    def winfo_rootx(self): return 0
    def winfo_rooty(self): return 0
    def winfo_width(self): return 100
    def winfo_height(self): return 40
    def __str__(self): return f"fw{id(self)}"


class FakeToplevel:
    live = []

    def __init__(self):
        self.destroyed = False
        FakeToplevel.live.append(self)

    def destroy(self):
        self.destroyed = True
        FakeToplevel.live.remove(self)


class Tip(cw._Tip):
    _hooked = set()

    def _make(self, x, y):
        return FakeToplevel()


class Tooltips(unittest.TestCase):
    def setUp(self):
        FakeToplevel.live.clear()
        cw._Tip.current = None
        Tip.current = None
        self.clock, self.top = Clock(), FakeTop()
        cw._Tip.current = None

    def mk(self, inside=True):
        w = FakeWidget(self.clock, self.top, inside)
        t = Tip(w, "hello")
        return w, t

    def test_shows_after_delay(self):
        # Enter, wait past the delay with the pointer still inside: exactly one tooltip appears.
        w, t = self.mk()
        w.fire("<Enter>")
        self.assertEqual(len(FakeToplevel.live), 0)
        self.clock.advance(Tip.TIP_DELAY_MS + 10)
        self.assertEqual(len(FakeToplevel.live), 1)

    def test_leave_before_delay_never_shows(self):
        # Enter then Leave before the delay fires: nothing is ever created.
        w, t = self.mk()
        w.fire("<Enter>"); w.fire("<Leave>")
        self.clock.advance(2000)
        self.assertEqual(FakeToplevel.live, [])

    def test_pointer_already_gone_when_delay_fires(self):
        # Missed Leave (pointer outside by the time the timer fires): no tooltip.
        w, t = self.mk(inside=False)
        w.fire("<Enter>")
        self.clock.advance(2000)
        self.assertEqual(FakeToplevel.live, [])

    def test_leave_destroys(self):
        w, t = self.mk()
        w.fire("<Enter>"); self.clock.advance(1000)
        w.fire("<Leave>")
        self.assertEqual(FakeToplevel.live, [])

    def test_click_destroys(self):
        # Enter, tooltip showing, then a click: destroyed (this is the stuck-tip scenario).
        w, t = self.mk()
        w.fire("<Enter>"); self.clock.advance(1000)
        w.fire("<ButtonPress>")
        self.assertEqual(FakeToplevel.live, [])

    def test_click_before_delay_cancels(self):
        w, t = self.mk()
        w.fire("<Enter>"); w.fire("<ButtonPress>")
        self.clock.advance(2000)
        self.assertEqual(FakeToplevel.live, [])

    def test_key_unmap_focusout_destroy_hide(self):
        for ev in ("<KeyPress>", "<Unmap>", "<FocusOut>", "<Destroy>"):
            w, t = self.mk()
            w.fire("<Enter>"); self.clock.advance(1000)
            self.assertEqual(len(FakeToplevel.live), 1, ev)
            w.fire(ev)
            self.assertEqual(FakeToplevel.live, [], ev)

    def test_window_events_hide(self):
        # Minimise / deactivate / lose focus / move or resize of the main window all hide it.
        for ev in ("<Unmap>", "<Deactivate>", "<FocusOut>", "<Configure>"):
            w, t = self.mk()
            w.fire("<Enter>"); self.clock.advance(1000)
            self.assertEqual(len(FakeToplevel.live), 1, ev)
            self.top.fire(ev)
            self.assertEqual(FakeToplevel.live, [], ev)

    def test_rapid_enter_on_two_widgets_only_one_tip(self):
        # Enter A, Enter B without Leave: A never shows, B shows once.
        a, ta = self.mk(); b, tb = self.mk()
        a.fire("<Enter>"); self.clock.advance(100)
        b.fire("<Enter>"); self.clock.advance(2000)
        self.assertEqual(len(FakeToplevel.live), 1)
        a.fire("<Enter>"); self.clock.advance(1000)      # A again while B showing: B destroyed, only A remains
        self.assertEqual(len(FakeToplevel.live), 1)

    def test_enter_twice_same_widget_no_orphan(self):
        w, t = self.mk()
        w.fire("<Enter>"); self.clock.advance(1000)
        w.fire("<Enter>"); self.clock.advance(1000)
        self.assertEqual(len(FakeToplevel.live), 1)
        w.fire("<Leave>")
        self.assertEqual(FakeToplevel.live, [])

    def test_failsafe_timer(self):
        # No Leave ever arrives: it still disappears by itself.
        w, t = self.mk()
        w.fire("<Enter>"); self.clock.advance(Tip.TIP_DELAY_MS + 10)
        self.assertEqual(len(FakeToplevel.live), 1)
        self.clock.advance(Tip.FAILSAFE_MS + 10)
        self.assertEqual(FakeToplevel.live, [])

    def test_hide_current_is_safe_when_idle(self):
        cw._Tip.hide_current()

    def test_all_users_use_the_class(self):
        # Every tooltip in chat_window.py goes through _Tip (no private Toplevel tooltips elsewhere).
        src = open(cw.__file__, encoding="utf-8").read()
        self.assertEqual(src.count("tk.Toplevel("), 2 if "tk.Toplevel(root)" in src else src.count("tk.Toplevel("))
        self.assertIn("_Tip(wdg, model.tip)", src)


if __name__ == "__main__":
    unittest.main()
