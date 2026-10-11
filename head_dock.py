"""Docks the 3D head inside the main AIDA window instead of floating over the desktop.

How it works
    * face3d.py already renders the head offscreen (ModernGL standalone context) and reads the pixels back into a
      premultiplied BGRA buffer. Floating mode hands that buffer to a layered window. Here it goes into a plain Tk
      Frame instead: the render thread composites the buffer over the panel colour with GDI AlphaBlend straight
      into the frame's own HWND (double-buffered, so no flicker). No re-parenting, no layered window, no second
      top-level window, and the soft glow blends into the dark panel.
    * The head blends over the window background colour exactly (no panel, border or header), so it appears to float.
    * Rendering pauses by itself when the main window is minimised, withdrawn or the dock is collapsed.
    * face3d.py is untouched: this module imports its Animator, HeadRenderer and classify_text.

Pure logic (fit_square, render_decision, DockController) has no Tk/GL and is covered by test_head_dock.py.
"""
import collections
import ctypes
import ctypes.wintypes as wt
import threading
import time

import face3d as F

GA_ROOT = 2
SRCCOPY = 0x00CC0020
HALFTONE = 4
BF_ALPHA_255 = 0x01FF0000          # BLENDFUNCTION: AC_SRC_OVER, flags 0, SourceConstantAlpha 255, AC_SRC_ALPHA


# ------------------------------------------------------------------ pure helpers
def dock_px(size, scale):
    """Edge of the (square) head in physical pixels: config size at 100% scaling times the DPI scale, even."""
    return max(120, int(float(size) * float(scale))) // 2 * 2


def fit_square(w, h):
    """Largest centred square inside a w x h client area -> (x, y, side). side 0 = nothing to draw."""
    side = min(int(w), int(h))
    if side < 8:
        return 0, 0, 0
    return (int(w) - side) // 2, (int(h) - side) // 2, side


def hex_to_colorref(hexcol):
    """'#RRGGBB' -> Win32 COLORREF (0x00BBGGRR)."""
    h = str(hexcol).lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (b << 16) | (g << 8) | r


def render_decision(visible, iconic, paused, w, h):
    """'run' when the head should be drawn this frame, otherwise the reason it is pausing."""
    if paused:
        return "paused"
    if iconic:
        return "minimised"
    if not visible:
        return "hidden"
    if min(w, h) < 8:
        return "tiny"
    return "run"


class DockController:
    """Keeps the dock region in step with the head switch. `ui` needs show(), hide(), set_collapsed(bool).
    The head is only 'docked' when it is on AND the live face object says docked (a floating fallback is not)."""

    def __init__(self, ui, collapsed=False, on_collapsed=None):
        self.ui, self.collapsed = ui, bool(collapsed)
        self.on_collapsed = on_collapsed or (lambda c: None)
        self.shown = False
        self.face = None

    def sync(self, on, face):
        self.face = face
        docked = bool(on and face is not None and getattr(face, "docked", False))
        if docked:
            self.ui.set_collapsed(self.collapsed)
            self.ui.show()
        else:
            self.ui.hide()
        self.shown = docked
        if face is not None and hasattr(face, "set_paused"):
            face.set_paused(self.collapsed or not docked)
        return docked

    def toggle_collapsed(self):
        self.collapsed = not self.collapsed
        self.on_collapsed(self.collapsed)
        if self.shown:
            self.ui.set_collapsed(self.collapsed)
            if self.face is not None and hasattr(self.face, "set_paused"):
                self.face.set_paused(self.collapsed)
        return self.collapsed


# ------------------------------------------------------------------ Win32 (own DLL instances, own signatures)
_u32 = ctypes.WinDLL("user32")
_g32 = ctypes.WinDLL("gdi32")
_m32 = ctypes.WinDLL("msimg32")
for _fn, _res, _args in [
    (_u32.GetDC, wt.HDC, [wt.HWND]), (_u32.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
    (_u32.GetClientRect, wt.BOOL, [wt.HWND, ctypes.POINTER(wt.RECT)]),
    (_u32.FillRect, ctypes.c_int, [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]),
    (_u32.IsIconic, wt.BOOL, [wt.HWND]), (_u32.IsWindowVisible, wt.BOOL, [wt.HWND]),
    (_u32.IsWindow, wt.BOOL, [wt.HWND]), (_u32.GetAncestor, wt.HWND, [wt.HWND, wt.UINT]),
    (_g32.CreateSolidBrush, wt.HBRUSH, [wt.COLORREF]), (_g32.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
    (_g32.CreateCompatibleDC, wt.HDC, [wt.HDC]), (_g32.DeleteDC, wt.BOOL, [wt.HDC]),
    (_g32.CreateCompatibleBitmap, wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
    (_g32.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
    (_g32.SetStretchBltMode, ctypes.c_int, [wt.HDC, ctypes.c_int]),
    (_g32.BitBlt, wt.BOOL, [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int,
                            ctypes.c_int, wt.DWORD]),
    (_m32.AlphaBlend, wt.BOOL, [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]),
]:
    _fn.restype, _fn.argtypes = _res, _args


# ------------------------------------------------------------------ the docked head
class DockedHead:
    """Drop-in for face3d.Face3D as far as Overlay is concerned (stop(), thread, _teardown(), dead, set_emotion(),
    stats()), but it draws into `frame` (a Tk Frame inside the main window). Raises if the GPU path cannot start so
    the caller can fall back to the floating head."""
    docked = True
    win = None

    def __init__(self, frame, scale, get_state, get_level, size=260, fps=30, shell="white", bg="#171B21",
                 get_text=None, auto_emotion=True, log=None):
        self.frame = frame
        self.get_state, self.get_level, self.get_text = get_state, get_level, get_text
        self.log = log or (lambda *a: None)
        self.size = dock_px(size, scale)
        self.fps = F._clamp(int(fps or 30), 10, 60)
        self.auto_emotion = bool(auto_emotion)
        self.shell = (F.SHELLS.get(str(shell).lower(), F.SHELLS["white"])
                      if not isinstance(shell, (tuple, list)) else tuple(shell))
        self.bg = hex_to_colorref(bg)
        self.anim = F.Animator()
        self.dead = False
        self.err = None
        self.gl_info = None
        self.pause_reason = None
        self.frame_ms = collections.deque(maxlen=900)
        self._run = True
        self._paused = False
        self._torn = False
        self._req = collections.deque()
        frame.update_idletasks()
        self.hwnd = frame.winfo_id()
        if not self.hwnd:
            raise RuntimeError("dock frame has no window handle")
        self.root_hwnd = _u32.GetAncestor(self.hwnd, GA_ROOT) or self.hwnd
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="head-dock-render", daemon=True)
        self.thread.start()
        if not self._ready.wait(10.0) or self.err is not None:
            self._run = False
            raise RuntimeError(f"3D head unavailable: {self.err or 'render thread did not start in time'}")
        self.log("face", f"3D head docked in the main window on {self.gl_info[0]} ({self.size}px, {self.fps} fps)")

    # -- public (any thread)
    def set_emotion(self, name, intensity=0.6, hold=None):
        self._req.append((name, intensity, hold))

    def set_paused(self, paused):
        self._paused = bool(paused)

    def stop(self):
        self._run = False

    def _teardown(self):
        self._run = False
        self._torn = True              # the frame belongs to the chat window: nothing to destroy here

    def stats(self):
        ms = sorted(self.frame_ms)
        if not ms:
            return None
        return dict(frames=len(ms), mean=sum(ms) / len(ms), p50=ms[len(ms) // 2], p95=ms[int(len(ms) * 0.95)],
                    max=ms[-1])

    # -- render thread
    def _client(self):
        rc = wt.RECT()
        if not _u32.GetClientRect(self.hwnd, ctypes.byref(rc)):
            return 0, 0
        return rc.right - rc.left, rc.bottom - rc.top

    def _decision(self):
        if not _u32.IsWindow(self.hwnd):
            return "gone"
        w, h = self._client()
        return render_decision(bool(_u32.IsWindowVisible(self.hwnd)), bool(_u32.IsIconic(self.root_hwnd)),
                               self._paused, w, h)

    def _blit(self, hdc_cache):
        """Fill with the panel colour, AlphaBlend the head on top, copy to the frame. Returns True when drawn."""
        w, h = self._client()
        x, y, side = fit_square(w, h)
        if not side:
            return False
        hdc = _u32.GetDC(self.hwnd)
        if not hdc:
            return False
        try:
            c = hdc_cache
            if c.get("size") != (w, h):
                if c.get("bmp"):
                    _g32.SelectObject(c["dc"], c["old"])
                    _g32.DeleteObject(c["bmp"])
                c["bmp"] = _g32.CreateCompatibleBitmap(hdc, w, h)
                c["old"] = _g32.SelectObject(c["dc"], c["bmp"])
                c["size"] = (w, h)
            rc = wt.RECT(0, 0, w, h)
            _u32.FillRect(c["dc"], ctypes.byref(rc), c["brush"])
            _m32.AlphaBlend(c["dc"], x, y, side, side, c["src"], 0, 0, self.size, self.size, BF_ALPHA_255)
            _g32.BitBlt(hdc, 0, 0, w, h, c["dc"], 0, 0, SRCCOPY)
            return True
        finally:
            _u32.ReleaseDC(self.hwnd, hdc)

    def _loop(self):
        r = None
        c = {}
        dib = None
        try:
            try:
                ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
            except Exception:
                pass
            r = F.HeadRenderer(self.size, self.shell)
            self.gl_info = r.info
            c["dc"] = _g32.CreateCompatibleDC(None)
            c["src"] = _g32.CreateCompatibleDC(None)
            c["brush"] = _g32.CreateSolidBrush(self.bg)
            _g32.SetStretchBltMode(c["dc"], HALFTONE)
            dib = F._DIB(c["src"], self.size)
            _g32.SelectObject(c["src"], dib.hbm)
            now = time.perf_counter()
            r.draw(self.anim.update(now, 0.016, "idle", 0.0)).read_into(dib.view)
        except Exception as e:
            self.err = e
            self._ready.set()
            self._cleanup(r, c, dib)
            return
        self._ready.set()
        last_tt = None
        if self.get_text:
            try:
                last_tt = self.get_text()[1]
            except Exception:
                last_tt = None
        period = 1.0 / self.fps
        last = time.perf_counter()
        fails = 0
        try:
            while self._run:
                f0 = time.perf_counter()
                dt, last = f0 - last, f0
                why = self._decision()
                if why != "run":
                    self.pause_reason = why
                    last = time.perf_counter()
                    time.sleep(0.25 if why != "gone" else 0.5)
                    continue
                self.pause_reason = None
                activity, level = "idle", 0.0
                try:
                    activity = self.get_state() or "idle"
                    level = float(self.get_level() or 0.0)
                except Exception:
                    pass
                if self.get_text and self.auto_emotion:
                    try:
                        txt, tt = self.get_text()
                        if tt != last_tt:
                            last_tt = tt
                            emo = F.classify_text(txt)
                            if emo:
                                self.anim.set_emotion(emo[0], emo[1], hold="speech", now=f0)
                    except Exception:
                        pass
                while self._req:
                    name, k, hold = self._req.popleft()
                    self.anim.set_emotion(name, k, hold, now=f0)
                r.draw(self.anim.update(f0, dt, activity, level)).read_into(dib.view)
                fails = 0 if self._blit(c) else fails + 1
                spent = time.perf_counter() - f0
                self.frame_ms.append(spent * 1000)
                time.sleep(max(0.002, period - spent) if fails < 30 else 0.5)
        except Exception as e:
            self.err = e
            self.dead = True
            try:
                self.log("face", f"3D head stopped: {e}")
            except Exception:
                pass
        finally:
            self._cleanup(r, c, dib)

    def _cleanup(self, r, c, dib):
        try:
            if r:
                r.release()
            if c.get("bmp"):
                _g32.SelectObject(c["dc"], c["old"])
                _g32.DeleteObject(c["bmp"])
            if c.get("brush"):
                _g32.DeleteObject(c["brush"])
            for k in ("dc", "src"):
                if c.get(k):
                    _g32.DeleteDC(c[k])
            if dib is not None:
                _g32.DeleteObject(dib.hbm)
        except Exception:
            pass


# ------------------------------------------------------------------ Tk side: the dock region in the chat window
class DockPanel:
    """Right-hand column of the main window: just the square the head is drawn into, with NO header, border, relief
    or padding colour of its own. Every widget uses the window background `bg` and the head is blended over that
    exact colour, so the head floats on the window with no visible box. Implements the ui interface DockController
    wants (show / hide / set_collapsed; collapsing is only used by tests and old saved state: there is no UI for it)."""

    def __init__(self, tk, parent, before, colours, scale, size, on_toggle=None):
        self.tk, self.s = tk, scale
        BG = colours[0]
        self.bg = self.PANEL = BG           # PANEL kept as the name voice_claude passes to DockedHead(bg=...)
        self.px = dock_px(size, scale)
        self.parent, self.before = parent, before
        self.pad = int(14 * scale)
        flat = dict(bg=BG, bd=0, highlightthickness=0, relief="flat", padx=0, pady=0)
        self.col = tk.Frame(parent, bg=BG, bd=0, highlightthickness=0)
        self.box = tk.Frame(self.col, width=self.px, height=self.px, bd=0, highlightthickness=0, bg=BG)
        self.box.pack_propagate(False)
        self.frame = tk.Frame(self.box, bg=BG, width=self.px, height=self.px, bd=0, highlightthickness=0)
        self.frame.pack()
        self.box.pack(side="top")
        self.visible = False
        self.collapsed = False

    def set_size(self, size):
        self.px = dock_px(size, self.s)
        self.box.configure(width=self.px, height=self.px)
        self.frame.configure(width=self.px, height=self.px)

    def show(self):
        if not self.visible:
            self.col.pack(side="right", fill="y", padx=(0, self.pad), pady=(int(6 * self.s), 0), before=self.before)
            self.visible = True
        self._apply()

    def hide(self):
        if self.visible:
            self.col.pack_forget()
            self.visible = False

    def set_collapsed(self, c):
        self.collapsed = bool(c)
        self._apply()

    def _apply(self):
        if self.collapsed:
            self.box.pack_forget()
        elif not self.box.winfo_manager():
            self.box.pack(side="top")
        self.col.update_idletasks()
