"""AIDA floating face: a small borderless always-on-top neon head that mimics AIDA speaking.

Runs on the Tk main thread of the existing status overlay (a Toplevel on the same root), so there is no extra
process or thread. It only READS state through two callables and never touches the audio or speech code:
    get_state() -> "idle" | "listening" | "thinking" | "speaking"
    get_level() -> live speech amplitude, 0.0 .. ~0.3 (RMS of the audio being played)
Every frame is wrapped in try/except; a face failure is logged once and the face just stops drawing.
"""
import math
import random
import time
import ctypes

KEY = "#010203"          # colour keyed out by Windows, so only the head shows
BODY = "#07121c"
COLOURS = {"idle": "#36d6ff", "listening": "#ff4d6d", "thinking": "#ffb02e", "speaking": "#3de8a0"}


def _mix(a, b, t):
    """Blend hex colour a toward b by t (0..1)."""
    a = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def work_area():
    """Primary screen work area (excludes the taskbar): left, top, right, bottom."""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
        r = RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)    # SPI_GETWORKAREA
        if r.r > r.l and r.b > r.t:
            return r.l, r.t, r.r, r.b
    except Exception:
        pass
    return None


class Face:
    FPS_MS = 33

    def __init__(self, root, scale, get_state, get_level, size=170, pos=None, opacity=0.85,
                 on_moved=None, log=None):
        import tkinter as tk
        self.tk, self.root, self.s = tk, root, scale
        self.get_state, self.get_level, self.on_moved = get_state, get_level, on_moved
        self.log = log or (lambda *a: None)
        self.size = max(80, int(size * scale))
        self.opacity = min(1.0, max(0.2, float(opacity)))
        self.dot = False                       # shrunk to a small dot
        self.dead = False
        self.t0 = time.time()
        self.lvl = 0.0                         # smoothed mouth opening 0..1
        self.col = COLOURS["idle"]
        self.blink_at = time.time() + 2.5
        self.blink_start = None
        self.glance = (0.0, 0.0)
        self.glance_to = (0.0, 0.0)
        self.glance_at = time.time() + 1.5
        self._drag = None
        self._moved_flag = False

        w = self.win = tk.Toplevel(root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=KEY)
        try:
            w.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        w.attributes("-alpha", self.opacity)
        self.cv = tk.Canvas(w, width=self.size, height=self.size, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        x, y = self._start_pos(pos)
        w.geometry(f"{self.size}x{self.size}+{x}+{y}")
        self.cv.bind("<ButtonPress-1>", self._grab)
        self.cv.bind("<B1-Motion>", self._move)
        self.cv.bind("<ButtonRelease-1>", self._drop)
        self.cv.bind("<Double-Button-1>", lambda e: self.toggle_dot())
        self.cv.bind("<Button-3>", self._menu)
        self.menu = tk.Menu(w, tearoff=0)
        self.menu.add_command(label="Hide / show face", command=self.toggle_dot)
        self.menu.add_command(label="Reset position", command=self.reset_pos)
        w.deiconify()
        w.update_idletasks()
        self._no_focus()
        self._frame()

    # -- window plumbing
    def _no_focus(self):
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.win.winfo_id())
            ex = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x00000080)    # NOACTIVATE | TOOLWINDOW
        except Exception:
            pass

    def _start_pos(self, pos):
        if pos and len(pos) == 2:
            return int(pos[0]), int(pos[1])
        return self.default_pos()

    def default_pos(self):
        wa = work_area() or (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        m = int(16 * self.s)
        return wa[2] - self.size - m, wa[3] - self.size - m

    def reset_pos(self):
        x, y = self.default_pos()
        self.win.geometry(f"+{x}+{y}")
        if self.on_moved:
            self.on_moved(None, None)

    def toggle_dot(self):
        self.dot = not self.dot
        d = max(24, int(26 * self.s))
        if self.dot:
            self._full = self.win.winfo_width()
            self.cv.config(width=d, height=d)
            self.win.geometry(f"{d}x{d}")
        else:
            self.cv.config(width=self.size, height=self.size)
            self.win.geometry(f"{self.size}x{self.size}")

    def _menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _grab(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
        self._moved_flag = False

    def _move(self, e):
        if self._drag:
            self._moved_flag = True
            self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drop(self, e):
        if self._drag and self._moved_flag and self.on_moved:
            self.on_moved(self.win.winfo_x(), self.win.winfo_y())
        self._drag = None

    # -- animation
    def _frame(self):
        if self.dead:
            return
        try:
            self._draw()
        except Exception as e:                  # never let the face take anything else down
            self.dead = True
            self.log("face", f"Face stopped: {e}")
            try:
                self.win.destroy()
            except Exception:
                pass
            return
        self.root.after(self.FPS_MS, self._frame)

    def _draw(self):
        now = time.time()
        t = now - self.t0
        state = self.get_state()
        raw = float(self.get_level() or 0.0)
        if state == "speaking":
            # real amplitude when the audio path provides it; otherwise a lively synthetic wobble
            target = min(1.0, raw * 7.0) if raw > 0.0005 else 0.0
        else:
            target = 0.0
        k = 0.6 if target > self.lvl else 0.25            # fast attack, slow release
        self.lvl += (target - self.lvl) * k
        goal = COLOURS.get(state, COLOURS["idle"])
        self.col = _mix(self.col, goal, 0.2)

        c, S, col = self.cv, self.size, self.col
        c.delete("all")
        if self.dot:
            r = S * 0 + c.winfo_reqwidth() / 2
            pulse = 0.5 + 0.5 * math.sin(t * 3)
            c.create_oval(2, 2, r * 2 - 2, r * 2 - 2, fill=BODY, outline=_mix(col, BODY, 0.5 - 0.3 * pulse), width=3)
            c.create_oval(r - 4, r - 4, r + 4, r + 4, fill=col, outline="")
            return

        breathe = 0.5 + 0.5 * math.sin(t * 1.6)
        cx = cy = S / 2
        hw, hh = S * 0.40, S * 0.40                       # head half size
        bob = math.sin(t * 1.6) * S * 0.008 * (1 + 2 * self.lvl)
        cy += bob

        # glow rings (dimmer as they go out), then the head body
        for i, (grow, mixv) in enumerate(((10, 0.86), (7, 0.75), (4, 0.6))):
            g = grow * (0.6 + 0.4 * breathe) * S / 170
            self._head(c, cx, cy, hw + g * 0.4, hh + g * 0.4, outline=_mix(col, BODY, mixv), width=max(2, g * 0.5))
        self._head(c, cx, cy, hw, hh, fill=BODY, outline=col, width=max(2, S / 60))

        # antenna
        ant = _mix(col, "#ffffff", 0.3 * breathe + 0.5 * self.lvl)
        c.create_line(cx, cy - hh, cx, cy - hh - S * 0.07, fill=col, width=max(2, S / 85))
        r = S * 0.025 * (1 + 0.5 * self.lvl)
        c.create_oval(cx - r, cy - hh - S * 0.07 - r, cx + r, cy - hh - S * 0.07 + r, fill=ant, outline="")

        # eyes: blink + glance
        if now >= self.blink_at and self.blink_start is None:
            self.blink_start = now
        open_ = 1.0
        if self.blink_start is not None:
            p = (now - self.blink_start) / 0.16
            if p >= 1:
                self.blink_start = None
                self.blink_at = now + random.uniform(2.0, 5.5)
            else:
                open_ = abs(2 * p - 1)
        if state == "thinking":                           # eyes sweep side to side
            gx, gy = math.sin(t * 2.2) * 0.9, -0.5
        elif state == "listening":
            gx, gy = 0.0, 0.0
        else:
            if now >= self.glance_at:
                self.glance_to = (random.uniform(-0.8, 0.8), random.uniform(-0.5, 0.5)) if random.random() < 0.7 else (0.0, 0.0)
                self.glance_at = now + random.uniform(1.2, 3.5)
            gx, gy = self.glance_to
        self.glance = (self.glance[0] + (gx - self.glance[0]) * 0.18, self.glance[1] + (gy - self.glance[1]) * 0.18)
        ex, ey = hw * 0.46, hh * 0.22
        ew, eh = S * 0.075, S * 0.085 * (1.25 if state == "listening" else 1.0)
        for sx in (-1, 1):
            x = cx + sx * ex + self.glance[0] * S * 0.03
            y = cy - ey + self.glance[1] * S * 0.025
            h = max(S * 0.008, eh * open_)
            c.create_rectangle(x - ew, y - h, x + ew, y + h, fill=_mix(col, BODY, 0.65), outline="")
            c.create_rectangle(x - ew * 0.75, y - h * 0.7, x + ew * 0.75, y + h * 0.7, fill=col, outline="")

        # mouth: waveform whose height follows the speech amplitude
        mw = hw * 1.0
        my = cy + hh * 0.45
        n = 28
        pts = []
        amp = hh * 0.42 * self.lvl
        for i in range(n + 1):
            u = i / n
            env = math.sin(math.pi * u) ** 0.8             # pinned at both ends
            wob = math.sin(t * 22 + u * 11) * 0.6 + math.sin(t * 13 - u * 7) * 0.4
            idle = math.sin(t * 2 + u * 5) * hh * 0.012
            pts += [cx - mw + 2 * mw * u, my + env * (wob * amp + idle)]
        c.create_line(*pts, fill=_mix(col, BODY, 0.6), width=max(5, S / 22), capstyle="round", joinstyle="round", smooth=True)
        c.create_line(*pts, fill=col, width=max(2, S / 55), capstyle="round", joinstyle="round", smooth=True)

    @staticmethod
    def _head(c, cx, cy, hw, hh, fill="", outline="", width=2):
        """Rounded hexagon-ish head: chamfered corners."""
        k = 0.28
        pts = [cx - hw * (1 - k), cy - hh, cx + hw * (1 - k), cy - hh, cx + hw, cy - hh * (1 - k),
               cx + hw, cy + hh * (1 - k), cx + hw * (1 - k), cy + hh, cx - hw * (1 - k), cy + hh,
               cx - hw, cy + hh * (1 - k), cx - hw, cy - hh * (1 - k)]
        c.create_polygon(*pts, fill=fill, outline=outline, width=width, joinstyle="round")
