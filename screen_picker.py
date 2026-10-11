"""Screen picker for 'Record screen': tick one or more screens, flash big numbers on the monitors to identify them.

    pick_screens(parent, monitors, on_done)

`monitors` is ObsRecorder.list_monitors()['monitors'] (dicts: index, name, rect [l,t,r,b], optionally number/label) or the
output of screen_record.label_monitors (no rect: the real Windows monitor rects are used for the Detect overlays).
`on_done` gets the chosen monitor indexes (sorted left to right) or None if cancelled. Tk thread only.

Pure helpers (no Tk, unit-tested): build_rows, default_selection, record_enabled, toggle, overlay_geometry.
"""
import ctypes
import re

SIDE_NAMES = {2: ["left", "right"], 3: ["left", "middle", "right"]}
DETECT_MS = 3000


# ------------------------------------------------------------------ pure parts
def build_rows(monitors, win_rects=None):
    """Numbered rows, leftmost first. Number = 1-based position by rect.left (ties: index). Without rects on every
    monitor, falls back to `win_rects` (Windows monitors sorted by left) matched by position, else sorts by index.
    Row: {index, number, side, rect, size, name, text}. `text` is the full label: 'Left screen (1920x1080)'."""
    mons = [m for m in (monitors or []) if isinstance(m, dict) and "index" in m]

    def has_rect(m):
        r = m.get("rect")
        return isinstance(r, (list, tuple)) and len(r) == 4

    if not all(has_rect(m) for m in mons):
        mons = sorted(mons, key=lambda m: m["index"])
        wr = sorted([list(r) for r in (win_rects or [])], key=lambda r: (r[0], r[1]))
        mons = [dict(m, rect=m["rect"] if has_rect(m) else (wr[i] if i < len(wr) and len(wr) == len(mons) else None))
                for i, m in enumerate(mons)]
        if all(m["rect"] for m in mons):
            mons.sort(key=lambda m: (m["rect"][0], m["index"]))
    else:
        mons = sorted(mons, key=lambda m: (m["rect"][0], m["index"]))
    names = SIDE_NAMES.get(len(mons))
    rows = []
    for i, m in enumerate(mons):
        r = m.get("rect")
        size = f"{int(r[2] - r[0])}x{int(r[3] - r[1])}" if r else str(m.get("size") or "")
        side = names[i] if names else None
        base = f"{side.capitalize()} screen" if side else f"Screen {i + 1}"
        rows.append({"index": m["index"], "number": i + 1, "side": side, "rect": list(r) if r else None,
                     "size": size, "name": str(m.get("name") or ""),
                     "text": f"{base} ({size})" if size else base})
    return rows


def default_selection(rows, window_rect=None):
    """Nothing ticked. Deliberate: recording the wrong screen silently is worse than one extra click, and the Record
    button stays disabled until the user ticks something. (window_rect is accepted so the policy can change in one place.)"""
    return set()


def toggle(selected, index):
    out = set(selected)
    out.symmetric_difference_update({index})
    return out


def record_enabled(selected):
    return bool(selected)


def chosen_indexes(rows, selected):
    """Selected monitor indexes in screen-number order (left to right)."""
    return [r["index"] for r in rows if r["index"] in selected]


DEFAULT_DEVICE = {"id": "default", "name": "Default"}
DEFAULT_PREFS = {"desktop": True, "mic": False, "audio": True, "record_dir": "", "mic_device": ""}


def effective_audio(master, desktop, mic):
    """The master 'Record audio' switch overrides the two tick boxes: off = both muted. -> (desktop, mic)."""
    return bool(master and desktop), bool(master and mic)


def mic_picker_enabled(master, mic):
    """The microphone dropdown only works when audio is on AND the mic box is ticked."""
    return bool(master and mic)


def device_names(devices):
    """Display names for the dropdown, in order; a duplicate name gets its id tail so the entries stay distinguishable."""
    devs = [d for d in (devices or []) if isinstance(d, dict) and d.get("id")] or [DEFAULT_DEVICE]
    names, seen = [], {}
    for d in devs:
        n = str(d.get("name") or d["id"])
        seen[n] = seen.get(n, 0) + 1
        names.append(n if seen[n] == 1 else "%s (%s)" % (n, str(d["id"])[-6:]))
    return names


def device_position(devices, device_id):
    """Index of `device_id` in the list, 0 (Default, listed first) when it is blank or no longer plugged in."""
    devs = [d for d in (devices or []) if isinstance(d, dict) and d.get("id")] or [DEFAULT_DEVICE]
    for i, d in enumerate(devs):
        if d["id"] == (device_id or "default"):
            return i
    return 0


def folder_message(path, warn):
    """'' or the plain warning for the Save-to folder. `warn` is obs_control.folder_warning (injected: no OBS here)."""
    try:
        return (warn(path) if warn else "") or ""
    except Exception:
        return ""


def overlay_geometry(rect, w, h):
    """Tk geometry string putting a w x h panel in the centre of monitor rect [l,t,r,b]."""
    l, t, r, b = rect
    x = int(l + (r - l - w) // 2)
    y = int(t + (b - t - h) // 2)
    return f"{w}x{h}{x:+d}{y:+d}"


def window_screen_number(rows, window_rect):
    """Number of the row whose rect contains the centre of window_rect, else None."""
    if not window_rect:
        return None
    cx, cy = (window_rect[0] + window_rect[2]) / 2, (window_rect[1] + window_rect[3]) / 2
    for r in rows:
        if r["rect"] and r["rect"][0] <= cx < r["rect"][2] and r["rect"][1] <= cy < r["rect"][3]:
            return r["number"]
    return None


# ------------------------------------------------------------------ Windows glue
def _win_rects():
    try:
        import obs_control
        return [m["rect"] for m in obs_control.win_monitors()]
    except Exception:
        return []


def _click_through(win):
    """Layered + transparent + no-activate + tool window, so the overlay never takes a click or focus."""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE = -20
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | 0x80000 | 0x20 | 0x08000000 | 0x80)
    except Exception:
        pass


def _round_corners(win, w, h, radius):
    try:
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        rgn = g.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
        u.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


# ------------------------------------------------------------------ dialog
def pick_screens(parent, monitors, on_done, options=None):
    """options (optional) adds the audio / Save-to / microphone section:
         {'prefs': {desktop, mic, audio, record_dir, mic_device}, 'devices': [{'id','name'}], 'warn': callable(path)->str}
    With options, on_done(chosen, settings) where settings = {audio, desktop, mic, record_dir, mic_device};
    without, on_done(chosen) as before."""
    import tkinter as tk
    from tkinter import ttk
    try:
        import chat_window as cw
        BG, PANEL, EDGE, FG, SUB, DIM, ACCENT = cw.BG, cw.PANEL, cw.EDGE, cw.FG, cw.SUB, cw.DIM, cw.ACCENT
        lighten = cw._lighten
    except Exception:
        BG, PANEL, EDGE, FG, SUB, DIM, ACCENT = "#111418", "#171B21", "#2A2F36", "#E8EBEF", "#AAB3BE", "#6F7883", "#3E82F7"
        lighten = lambda c: c
    s = parent.winfo_fpixels("1i") / 96.0
    rows = build_rows(monitors, _win_rects())
    if not rows:
        on_done(None)
        return None
    try:
        wrect = [parent.winfo_rootx(), parent.winfo_rooty(), parent.winfo_rootx() + parent.winfo_width(),
                 parent.winfo_rooty() + parent.winfo_height()]
    except Exception:
        wrect = None
    selected = set(default_selection(rows, wrect))
    state = {"done": False, "overlays": [], "after": None}

    dlg = tk.Toplevel(parent)
    dlg.title("Record which screen?")
    dlg.configure(bg=BG)
    dlg.transient(parent)
    dlg.resizable(False, False)
    pad = int(16 * s)
    W = int(600 * s)
    dlg.columnconfigure(2, weight=1)

    tk.Label(dlg, text="Record which screen?", bg=BG, fg=FG, font=("Segoe UI Semibold", 11),
             padx=pad, pady=int(10 * s)).grid(row=0, column=0, columnspan=4, sticky="w")
    tk.Label(dlg, text="Tick one or more, then press Record. Detect flashes a big number on each monitor.",
             bg=BG, fg=SUB, font=("Segoe UI", 9), wraplength=W - 2 * pad, justify="left",
             padx=pad).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, int(8 * s)))

    boxes = {}

    def paint():
        for r in rows:
            on = r["index"] in selected
            b = boxes[r["index"]]
            b.configure(text="☑" if on else "☐", fg=ACCENT if on else DIM)
        rec.configure(bg=ACCENT if record_enabled(selected) else PANEL,
                      fg="#FFFFFF" if record_enabled(selected) else DIM,
                      cursor="hand2" if record_enabled(selected) else "arrow")
        rec._base = ACCENT if record_enabled(selected) else PANEL

    def flip(index):
        nonlocal selected
        selected = toggle(selected, index)
        paint()

    gr = 2
    for r in rows:
        i = r["index"]
        click = lambda e, i=i: flip(i)
        box = tk.Label(dlg, text="☐", bg=PANEL, fg=DIM, font=("Segoe UI Symbol", 20), cursor="hand2",
                       padx=int(12 * s))
        box.grid(row=gr, column=0, rowspan=2, sticky="nsew", padx=(pad, 0), pady=(0, int(6 * s)))
        num = tk.Label(dlg, text=str(r["number"]), bg=PANEL, fg=FG, font=("Segoe UI Semibold", 22), width=2,
                       cursor="hand2")
        num.grid(row=gr, column=1, rowspan=2, sticky="nsew", pady=(0, int(6 * s)))
        txt = tk.Label(dlg, text=r["text"], bg=PANEL, fg=FG, font=("Segoe UI Semibold", 11), anchor="w",
                       justify="left", wraplength=W - int(150 * s), padx=int(10 * s), pady=int(4 * s), cursor="hand2")
        txt.grid(row=gr, column=2, sticky="nsew", padx=(0, pad))
        nm = tk.Label(dlg, text=r["name"], bg=PANEL, fg=SUB, font=("Segoe UI", 9), anchor="nw", justify="left",
                      wraplength=W - int(150 * s), padx=int(10 * s), pady=0, cursor="hand2")
        nm.grid(row=gr + 1, column=2, sticky="nsew", padx=(0, pad), pady=(0, int(6 * s)))
        for w in (box, num, txt, nm):
            w.bind("<Button-1>", click)
        boxes[i] = box
        gr += 2

    # -- finishing
    def clear_overlays():
        if state["after"]:
            try:
                dlg.after_cancel(state["after"])
            except Exception:
                pass
            state["after"] = None
        for o in state["overlays"]:
            try:
                o.destroy()
            except Exception:
                pass
        state["overlays"] = []

    def finish(result):
        if state["done"]:
            return
        state["done"] = True
        clear_overlays()
        final = settings()
        try:
            dlg.destroy()
        except Exception:
            pass
        if result is not None and options is not None:
            on_done(result, final)
        else:
            on_done(result)

    def do_record():
        if record_enabled(selected):
            finish(chosen_indexes(rows, selected))

    def do_cancel():
        finish(None)

    def pick(index):                       # one-click helper (kept for callers/tests that pick a single screen)
        finish([index])

    def detect():
        clear_overlays()
        pw = ph = int(360 * s)
        for r in rows:
            if not r["rect"]:
                continue
            o = tk.Toplevel(dlg)
            o.overrideredirect(True)
            o.attributes("-topmost", True)
            o.configure(bg="#0B0D10")
            o.geometry(overlay_geometry(r["rect"], pw, ph))
            tk.Label(o, text=str(r["number"]), bg="#0B0D10", fg="#4DB1FF",
                     font=("Segoe UI Semibold", -int(240 * s))).place(relx=0.5, rely=0.5, anchor="center")
            o.update_idletasks()
            try:
                o.attributes("-alpha", 0.82)
            except Exception:
                pass
            _round_corners(o, pw, ph, int(48 * s))
            _click_through(o)
            state["overlays"].append(o)
        state["after"] = dlg.after(DETECT_MS, clear_overlays)

    # -- audio / save-to / microphone (only when the caller supplies options)
    prefs = dict(DEFAULT_PREFS, **((options or {}).get("prefs") or {}))
    devices = (options or {}).get("devices") or [DEFAULT_DEVICE]
    warn_fn = (options or {}).get("warn")
    cur = {"audio": bool(prefs["audio"]), "desktop": bool(prefs["desktop"]), "mic": bool(prefs["mic"]),
           "record_dir": str(prefs["record_dir"] or ""), "mic_device": str(prefs["mic_device"] or "")}
    cur["mic_device"] = devices[device_position(devices, cur["mic_device"])]["id"] if devices else ""

    def settings():
        return dict(cur)

    checks, extra = {}, {}

    def paint_extra():
        if options is None:
            return
        for key, (lbl, label) in checks.items():
            on = cur[key]
            usable = key == "audio" or cur["audio"]
            lbl.configure(text=("☑ " if on else "☐ ") + label,
                          fg=(ACCENT if on else FG) if usable else DIM, cursor="hand2" if usable else "arrow")
        en = mic_picker_enabled(cur["audio"], cur["mic"])
        combo.configure(state="readonly" if en else "disabled")
        warn_lbl.configure(text=folder_message(cur["record_dir"], warn_fn))
        dir_lbl.configure(text=cur["record_dir"] or "(OBS's own folder)")

    def flip_check(key):
        if key != "audio" and not cur["audio"]:
            return                                   # greyed out while the master switch is off
        cur[key] = not cur[key]
        paint_extra()

    def set_folder(path):
        cur["record_dir"] = str(path or "")
        paint_extra()

    def browse():
        from tkinter import filedialog
        p = filedialog.askdirectory(parent=dlg, title="Save recordings to", initialdir=cur["record_dir"] or None,
                                    mustexist=False)
        if p:
            set_folder(p.replace("/", "\\"))

    def set_device(device_id):
        cur["mic_device"] = devices[device_position(devices, device_id)]["id"]
        combo.current(device_position(devices, device_id))

    if options is not None:
        tk.Frame(dlg, bg=EDGE, height=1).grid(row=gr, column=0, columnspan=4, sticky="ew", padx=pad,
                                              pady=(int(4 * s), int(8 * s)))
        gr += 1
        for key, label, indent in (("audio", "Record audio", 0), ("desktop", "Record PC sound", 1),
                                   ("mic", "Record microphone", 1)):
            lbl = tk.Label(dlg, bg=BG, fg=FG, font=("Segoe UI Semibold", 10), anchor="w", cursor="hand2")
            lbl.grid(row=gr, column=0, columnspan=4, sticky="w", padx=(pad + indent * int(28 * s), pad))
            lbl.bind("<Button-1>", lambda e, k=key: flip_check(k))
            checks[key] = (lbl, label)
            gr += 1
        tk.Label(dlg, text="Microphone", bg=BG, fg=SUB, font=("Segoe UI", 9), anchor="w").grid(
            row=gr, column=0, columnspan=2, sticky="w", padx=(pad + int(28 * s), 0), pady=(0, int(4 * s)))
        combo = ttk.Combobox(dlg, values=device_names(devices), state="readonly", width=48)
        combo.grid(row=gr, column=2, columnspan=2, sticky="ew", padx=(0, pad), pady=(0, int(4 * s)))
        combo.current(device_position(devices, cur["mic_device"]))
        combo.bind("<<ComboboxSelected>>", lambda e: cur.update(mic_device=devices[combo.current()]["id"]))
        gr += 1
        tk.Label(dlg, text="Save to", bg=BG, fg=SUB, font=("Segoe UI", 9), anchor="w").grid(
            row=gr, column=0, columnspan=2, sticky="w", padx=(pad, 0), pady=(int(6 * s), 0))
        dir_lbl = tk.Label(dlg, bg=PANEL, fg=FG, font=("Segoe UI", 9), anchor="w", justify="left",
                           wraplength=W - int(190 * s), padx=int(8 * s), pady=int(4 * s))
        dir_lbl.grid(row=gr, column=2, sticky="ew", pady=(int(6 * s), 0))
        br = tk.Label(dlg, text="Browse...", bg=EDGE, fg=FG, cursor="hand2", font=("Segoe UI Semibold", 9),
                      padx=int(10 * s), pady=int(4 * s))
        br.bind("<Button-1>", lambda e: browse())
        br.grid(row=gr, column=3, sticky="e", padx=(int(6 * s), pad), pady=(int(6 * s), 0))
        gr += 1
        warn_lbl = tk.Label(dlg, text="", bg=BG, fg="#F0B429", font=("Segoe UI", 9), anchor="w", justify="left",
                            wraplength=W - 2 * pad)
        warn_lbl.grid(row=gr, column=0, columnspan=4, sticky="w", padx=pad)
        gr += 1

    bar = tk.Frame(dlg, bg=BG)
    bar.grid(row=gr, column=0, columnspan=4, sticky="ew", padx=pad, pady=(int(6 * s), int(14 * s)))

    def bar_button(text, cmd, bg, fg, side):
        b = tk.Label(bar, text=text, bg=bg, fg=fg, cursor="hand2", width=10, pady=int(7 * s),
                     font=("Segoe UI Semibold", 10))
        b._base = bg
        b.bind("<Enter>", lambda e: b.configure(bg=lighten(b._base)) if (b is not rec or record_enabled(selected)) else None)
        b.bind("<Leave>", lambda e: b.configure(bg=b._base))
        b.bind("<Button-1>", lambda e: cmd())
        b.pack(side=side, padx=(int(8 * s), 0) if side == "right" else 0)
        return b

    det = bar_button("Detect", detect, EDGE, FG, "left")
    rec = bar_button("Record", do_record, PANEL, DIM, "right")
    cancel = bar_button("Cancel", do_cancel, EDGE, FG, "right")

    dlg.pick = pick
    dlg.cancel = do_cancel
    dlg.record = do_record
    dlg.detect = detect
    dlg.rows = rows
    dlg.toggle_index = flip
    dlg.settings = settings
    dlg.toggle_option = flip_check
    dlg.set_folder = set_folder
    dlg.set_device = set_device
    dlg.warning_text = (lambda: warn_lbl.cget("text")) if options is not None else (lambda: "")
    dlg.combo_state = (lambda: str(combo.cget("state"))) if options is not None else (lambda: "")
    dlg.protocol("WM_DELETE_WINDOW", do_cancel)
    dlg.bind("<Escape>", lambda e: do_cancel())
    paint()
    paint_extra()
    dlg.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + int(60 * s)
    dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
    dlg.focus_set()
    return dlg
