"""Tk side of the key settings: a slim dark 'Settings' menu strip under the title bar and the key-capture dialog.
All logic (parsing, validation, state) lives in hotkeys.py; this file only draws it.

The window's `app` must offer: hotkey_bindings() -> {'ptt','cancel'}, set_hotkey(name, spec) (raises RuntimeError on
failure), reset_hotkeys(). Without those (tests, old fakes) build_menubar returns None and nothing is drawn.
"""
import ctypes

import hotkeys as hk


def app_supports(app):
    return all(hasattr(app, n) for n in ("hotkey_bindings", "set_hotkey", "reset_hotkeys"))


def _win_down():
    try:
        g = ctypes.windll.user32.GetAsyncKeyState
        return bool(g(0x5B) & 0x8000 or g(0x5C) & 0x8000)
    except Exception:
        return False


class MenuStrip:
    """Slim strip with a 'Settings' drop-down. `c` = dict of colours: bg, panel, edge, fg, sub, dim, accent, error."""

    def __init__(self, tk, parent, c, scale, app, report):
        self.tk, self.c, self.s, self.app, self.report = tk, c, scale, app, report
        s = scale
        self.frame = tk.Frame(parent, bg=c["panel"])
        self.btn = tk.Label(self.frame, text="Settings", bg=c["panel"], fg=c["sub"], cursor="hand2",
                            font=("Segoe UI", 9), padx=int(12 * s), pady=int(3 * s))
        self.btn.pack(side="left")
        self.btn.bind("<Enter>", lambda e: self.btn.configure(bg=c["edge"], fg=c["fg"]))
        self.btn.bind("<Leave>", lambda e: self.btn.configure(bg=c["panel"], fg=c["sub"]))
        self.btn.bind("<Button-1>", lambda e: self.popup())
        self.menu = tk.Menu(self.frame, tearoff=0, bg=c["panel"], fg=c["fg"], activebackground=c["accent"],
                            activeforeground="#FFFFFF", bd=0, relief="flat", font=("Segoe UI", 9),
                            activeborderwidth=0, postcommand=self.refresh)
        self.menu.add_command(label="", command=lambda: self.capture("ptt"))
        self.menu.add_command(label="", command=lambda: self.capture("cancel"))
        self.menu.add_separator()
        self.menu.add_command(label="Reset keys to defaults", command=self.reset)
        self.menu.add_separator()
        self.menu.add_command(label="Play test sounds", command=self.test_sounds)
        self.refresh()

    def labels(self):
        b = self.app.hotkey_bindings()
        return [f"Push-to-talk key...    [{hk.display(b['ptt'])}]", f"Cancel key...    [{hk.display(b['cancel'])}]",
                None, "Reset keys to defaults"]

    def refresh(self):
        lab = self.labels()
        self.menu.entryconfigure(0, label=lab[0])
        self.menu.entryconfigure(1, label=lab[1])

    def popup(self):
        self.refresh()
        x, y = self.btn.winfo_rootx(), self.btn.winfo_rooty() + self.btn.winfo_height()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def capture(self, which):
        CaptureDialog(self.tk, self.frame.winfo_toplevel(), self.c, self.s, self.app, which, self.report)

    def test_sounds(self):
        fn = getattr(self.app, "play_test_sounds", None)
        if fn:
            fn()
        else:
            self.report("error", "This build has no test sounds.")

    def reset(self):
        try:
            self.app.reset_hotkeys()
            b = self.app.hotkey_bindings()
            self.report("system", f"Keys reset: push-to-talk {hk.display(b['ptt'])}, cancel {hk.display(b['cancel'])}.")
        except Exception as e:
            self.report("error", f"Could not reset the keys: {e}")
        self.refresh()


class CaptureDialog:
    """Modal 'press the key' dialog. Save / Use anyway / Clear / Cancel; Esc aborts."""

    def __init__(self, tk, owner, c, scale, app, which, report, win_down=_win_down):
        self.tk, self.c, self.app, self.which, self.report, self._win = tk, c, app, which, report, win_down
        s = scale
        b = app.hotkey_bindings()
        self.model = hk.CaptureModel(which, b[which], b["cancel" if which == "ptt" else "ptt"])
        d = self.win = tk.Toplevel(owner)
        d.title(hk.LABELS[which])
        d.configure(bg=c["bg"])
        d.transient(owner)
        d.resizable(False, False)
        pad = int(18 * s)
        body = tk.Frame(d, bg=c["bg"], padx=pad, pady=pad)
        body.pack()
        tk.Label(body, text=hk.LABELS[which], bg=c["bg"], fg=c["fg"], font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(body, text="Press the key or combination you want to use", bg=c["bg"], fg=c["sub"],
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(int(4 * s), int(10 * s)))
        self.shown = tk.Label(body, text=hk.display(b[which]), bg=c["panel"], fg=c["accent"], width=22,
                              font=("Segoe UI Semibold", 16), pady=int(12 * s), highlightthickness=1,
                              highlightbackground=c["edge"])
        self.shown.pack(fill="x")
        tk.Label(body, text="Current binding shown until you press something.", bg=c["bg"], fg=c["dim"],
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(int(3 * s), 0))
        self.msg = tk.Label(body, text=" ", bg=c["bg"], fg=c["sub"], font=("Segoe UI", 9), wraplength=int(340 * s),
                            justify="left", anchor="w")
        self.msg.pack(fill="x", pady=(int(8 * s), int(8 * s)))
        row = tk.Frame(body, bg=c["bg"])
        row.pack(fill="x")
        self.b_save = self._btn(row, "Save", self.save, c["accent"], "#FFFFFF")
        self.b_any = self._btn(row, "Use anyway", self.use_anyway, c["edge"], c["fg"])
        self.b_clear = self._btn(row, "Clear", self.clear, c["edge"], c["fg"])
        self.b_cancel = self._btn(row, "Cancel", self.close, c["edge"], c["fg"])
        for b_ in (self.b_save, self.b_clear, self.b_cancel):
            b_.pack(side="left", padx=(0, int(8 * s)))
        d.bind("<KeyPress>", self._key)
        d.bind("<Escape>", lambda e: (self.close(), "break")[1])
        d.protocol("WM_DELETE_WINDOW", self.close)
        d.update_idletasks()
        ox, oy = owner.winfo_rootx(), owner.winfo_rooty()
        d.geometry(f"+{ox + int(80 * s)}+{oy + int(120 * s)}")
        try:                                   # dark title bar like the main window
            import chat_window
            chat_window.style_titlebar(chat_window._hwnd_of(d))
        except Exception:
            pass
        d.focus_force()
        try:
            d.grab_set()
        except Exception:
            pass
        self.render()

    def _btn(self, parent, text, cmd, bg, fg):
        tk = self.tk
        w = tk.Label(parent, text=text, bg=bg, fg=fg, cursor="hand2", padx=14, pady=5, font=("Segoe UI", 10))
        w.bind("<Button-1>", lambda e: cmd())
        w._bg, w._on = bg, True
        return w

    def _key(self, e):
        if e.keysym == "Escape" and not (e.state & (hk.ST_CTRL | hk.ST_ALT)):
            return None
        self.model.key(e.keysym, e.state, self._win())
        self.render()
        return "break"

    def render(self):
        m, c = self.model, self.c
        if m.spec is None:
            self.shown.configure(text=hk.display(self.model.current) if not m.verdict.message else "-", fg=c["accent"])
        else:
            self.shown.configure(text=hk.display(m.spec) if m.spec else "(cleared)", fg=c["accent"])
        colour = {"ok": c["sub"], "warn": "#D9A93A", "error": c["error"]}[m.verdict.level]
        self.msg.configure(text=m.verdict.message or " ", fg=colour)
        self.b_save.configure(bg=c["accent"] if m.can_save else c["edge"], fg="#FFFFFF" if m.can_save else c["dim"])
        if m.needs_override:
            self.b_any.pack(side="left", padx=(0, 8), before=self.b_clear)
        else:
            self.b_any.pack_forget()
        if self.which == "ptt":
            self.b_clear.configure(fg=c["dim"])

    def use_anyway(self):
        self.model.use_anyway()
        self.render()

    def clear(self):
        self.model.clear()
        self.render()

    def save(self):
        spec = self.model.result()
        if spec is None:
            return
        try:
            self.app.set_hotkey(self.which, spec)
        except Exception as e:
            self.model.verdict = hk.Verdict("error", str(e))
            self.report("error", f"{hk.LABELS[self.which]}: {e}")
            self.render()
            return
        self.report("system", f"{hk.LABELS[self.which]} is now {hk.display(spec)}." if spec else
                    f"{hk.LABELS[self.which]} switched off.")
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()


def build_menubar(tk, parent, c, scale, app, report):
    """Pack the Settings strip at the top of `parent`. Returns it, or None when `app` has no hotkey support."""
    if not app_supports(app):
        return None
    strip = MenuStrip(tk, parent, c, scale, app, report)
    strip.frame.pack(fill="x", side="top")
    return strip
