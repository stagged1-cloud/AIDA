"""winctl.py - JSON-in/JSON-out control of native Windows apps via UI Automation.

Prefers methods that never move the real mouse (UIA patterns, window messages).
Real input is used ONLY when --allow-real-input is passed, and is always reported.
Every command prints exactly one JSON object on stdout and exits 0 (ok) or 1 (not ok).

Usage: python winctl.py <command> [args]   (see winctl.md)
"""
import argparse
import contextlib
import ctypes
import json
import sys
import time
from ctypes import wintypes

# --- DPI awareness must be set before any window/monitor/UIA call ----------------
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:  # pragma: no cover - older Windows
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

# =============================================================================
# Pure helpers (unit-tested without any UI)
# =============================================================================
SELECTOR_KEYS = {"name", "name~", "id", "type", "class", "index"}


class WinctlError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _split_terms(s):
    """Split on commas that are not inside single/double quotes."""
    terms, cur, quote = [], [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            terms.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if quote:
        raise WinctlError("bad_selector", "unterminated quote")
    terms.append("".join(cur))
    return [t.strip() for t in terms if t.strip()]


def parse_selector(s):
    """Parse a control selector.

    Forms: 'name=Equals,type=Button', 'id=num2Button', 'name~=sub', 'class=Edit',
    'index=1' (nth match, 0-based), or a bare word = name contains OR id equals.
    """
    if s is None or not str(s).strip():
        raise WinctlError("bad_selector", "empty selector")
    s = str(s).strip()
    sel = {}
    if "=" not in s:
        return {"any": s}
    for term in _split_terms(s):
        if "=" not in term:
            raise WinctlError("bad_selector", f"term without '=': {term!r}")
        k, v = term.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k not in SELECTOR_KEYS:
            raise WinctlError("bad_selector", f"unknown key {k!r}; valid: {sorted(SELECTOR_KEYS)}")
        if k == "index":
            try:
                v = int(v)
            except ValueError:
                raise WinctlError("bad_selector", f"index must be an integer, got {v!r}")
            if v < 0:
                raise WinctlError("bad_selector", "index must be >= 0")
        sel[k] = v
    return sel


def selector_matches(sel, name, auto_id, type_name, class_name):
    """Match a parsed selector (ignoring 'index') against control properties."""
    t = (type_name or "").lower()
    if t.endswith("control"):
        t = t[:-7]
    name_l, id_l, cls_l = (name or "").lower(), (auto_id or "").lower(), (class_name or "").lower()
    if "any" in sel:
        w = sel["any"].lower()
        return w in name_l or w == id_l
    if "name" in sel and name_l != sel["name"].lower():
        return False
    if "name~" in sel and sel["name~"].lower() not in name_l:
        return False
    if "id" in sel and id_l != sel["id"].lower():
        return False
    if "type" in sel:
        want = sel["type"].lower()
        want = want[:-7] if want.endswith("control") else want
        if t != want:
            return False
    if "class" in sel and cls_l != sel["class"].lower():
        return False
    return True


def parse_window_selector(s):
    """'hwnd:123' / 'pid:42' / 'process:notepad.exe' / anything else = title substring."""
    if s is None or not str(s).strip():
        raise WinctlError("bad_window_selector", "empty window selector")
    s = str(s).strip()
    low = s.lower()
    for prefix, kind in (("hwnd:", "hwnd"), ("pid:", "pid")):
        if low.startswith(prefix):
            try:
                return {kind: int(s[len(prefix):], 0)}
            except ValueError:
                raise WinctlError("bad_window_selector", f"{kind} must be an integer: {s!r}")
    if low.startswith("process:"):
        return {"process": s[8:].strip().lower()}
    return {"title": s}


def window_matches(sel, win):
    if "hwnd" in sel:
        return win["hwnd"] == sel["hwnd"]
    if "pid" in sel:
        return win["pid"] == sel["pid"]
    if "process" in sel:
        p = (win.get("process") or "").lower()
        return p == sel["process"] or p == sel["process"] + ".exe"
    return sel["title"].lower() in (win.get("title") or "").lower()


def _overlap(rect, mon_rect):
    l, t, r, b = rect
    ml, mt, mr, mb = mon_rect
    w = min(r, mr) - max(l, ml)
    h = min(b, mb) - max(t, mt)
    return max(w, 0) * max(h, 0)


def monitor_for_rect(rect, monitors):
    """Monitor number (1-based, left to right) with the largest overlap with rect."""
    best, best_area = None, 0
    for m in monitors:
        a = _overlap(rect, m["rect"])
        if a > best_area:
            best, best_area = m["number"], a
    return best


def number_monitors(raw):
    """raw: list of dicts with rect/primary/dpi. Sort left-to-right (then top), number from 1,
    and label left/middle/right where that makes sense."""
    mons = sorted(raw, key=lambda m: (m["rect"][0], m["rect"][1]))
    labels = {1: ["only"], 2: ["left", "right"], 3: ["left", "middle", "right"]}.get(len(mons))
    out = []
    for i, m in enumerate(mons, 1):
        d = dict(m)
        d["number"] = i
        d["label"] = labels[i - 1] if labels else f"monitor{i}"
        d["scale"] = dpi_scale(d.get("dpi", 96))
        out.append(d)
    return out


def dpi_scale(dpi):
    """Effective DPI -> scale factor (96 dpi = 1.0, 144 = 1.5)."""
    return round((dpi or 96) / 96.0, 4)


def logical_to_physical(value, scale):
    return int(round(value * scale))


def physical_to_logical(value, scale):
    return int(round(value / scale))


def rect_relative_to_monitor(rect, mon_rect):
    """Window rect (virtual-screen physical px) -> rect relative to a monitor's origin."""
    ml, mt = mon_rect[0], mon_rect[1]
    return [rect[0] - ml, rect[1] - mt, rect[2] - ml, rect[3] - mt]


def result(command, ok, method=None, cursor_before=None, cursor_after=None,
           real_input_used=False, **extra):
    """Standard result envelope. cursor_touched is true if the cursor moved or real input was used."""
    moved = (cursor_before is not None and cursor_after is not None and
             tuple(cursor_before) != tuple(cursor_after))
    d = {
        "ok": bool(ok),
        "command": command,
        "method": method,
        "real_input_used": bool(real_input_used),
        "cursor_touched": bool(moved or real_input_used),
        "cursor_before": list(cursor_before) if cursor_before is not None else None,
        "cursor_after": list(cursor_after) if cursor_after is not None else None,
    }
    d.update(extra)
    return d


# =============================================================================
# Win32 layer
# =============================================================================
def get_cursor():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def set_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


def list_monitors():
    raw = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    def cb(hmon, hdc, lprc, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        dx, dy = ctypes.c_uint(96), ctypes.c_uint(96)
        try:
            ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        except Exception:
            pass
        r = info.rcMonitor
        raw.append({"device": info.szDevice, "rect": [r.left, r.top, r.right, r.bottom],
                    "primary": bool(info.dwFlags & 1), "dpi": dx.value})
        return 1

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return number_monitors(raw)


def _process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(h)
    return None


def _frame_rect(hwnd):
    r = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS = 9 (visible bounds, excludes invisible resize borders)
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return [r.left, r.top, r.right, r.bottom]
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def list_windows(monitors=None, include_untitled=False):
    monitors = monitors if monitors is not None else list_monitors()
    out = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if cloaked.value:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if not title and not include_untitled:
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = _frame_rect(hwnd)
        out.append({"title": title, "process": _process_name(pid.value), "pid": pid.value,
                    "hwnd": int(hwnd), "monitor": monitor_for_rect(rect, monitors),
                    "rect": rect, "minimised": bool(user32.IsIconic(hwnd))})
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def resolve_window(selector_str):
    sel = parse_window_selector(selector_str)
    matches = [w for w in list_windows() if window_matches(sel, w)]
    if not matches:
        raise WinctlError("window_not_found", f"no visible window matches {selector_str!r}")
    return matches[0], len(matches)


# =============================================================================
# UI Automation layer
# =============================================================================
_auto = None


def uia():
    global _auto
    if _auto is None:
        import uiautomation as a
        a.SetGlobalSearchTimeout(2)
        try:
            a.Logger.SetLogFile(None) if hasattr(a.Logger, "SetLogFile") else None
        except Exception:
            pass
        _auto = a
    return _auto


def root_control(win):
    c = uia().ControlFromHandle(win["hwnd"])
    if c is None:
        raise WinctlError("uia_unavailable", "UIA returned no element for this window")
    return c


def _props(c):
    return c.Name, c.AutomationId, c.ControlTypeName, c.ClassName


def _rect(c):
    try:
        r = c.BoundingRectangle
        return [r.left, r.top, r.right, r.bottom]
    except Exception:
        return None


def find_control(root, selector_str, max_depth=14):
    sel = parse_selector(selector_str)
    want = sel.get("index", 0)
    seen = [0]

    def walk(c, depth):
        for ch in c.GetChildren():
            if selector_matches(sel, *_props(ch)):
                if seen[0] == want:
                    return ch
                seen[0] += 1
            if depth < max_depth:
                r = walk(ch, depth + 1)
                if r is not None:
                    return r
        return None

    found = walk(root, 1)
    if found is None:
        raise WinctlError("control_not_found",
                          f"no control matches {selector_str!r} (depth<={max_depth}); use 'tree' to inspect")
    return found


def supported_patterns(c):
    names = ["Invoke", "Value", "Toggle", "SelectionItem", "ExpandCollapse", "Text", "LegacyIAccessible"]
    ok = []
    for n in names:
        try:
            if getattr(c, f"Get{n}Pattern")() is not None:
                ok.append(n)
        except Exception:
            pass
    return ok


def build_tree(c, depth, max_depth, with_patterns):
    node = {"name": c.Name, "id": c.AutomationId,
            "type": c.ControlTypeName.replace("Control", ""), "class": c.ClassName,
            "rect": _rect(c)}
    if with_patterns:
        node["patterns"] = supported_patterns(c)
    if depth < max_depth:
        node["children"] = [build_tree(ch, depth + 1, max_depth, with_patterns) for ch in c.GetChildren()]
    return node


# =============================================================================
# Commands. Each returns a result dict; WinctlError is converted in main().
# =============================================================================
def cmd_list_windows(args):
    mons = list_monitors()
    wins = list_windows(mons, include_untitled=args.all)
    return result("list-windows", True, method="EnumWindows", windows=wins, monitors=mons,
                  cursor_before=get_cursor(), cursor_after=get_cursor())


def cmd_tree(args):
    before = get_cursor()
    win, _ = resolve_window(args.window)
    root = root_control(win)
    tree = build_tree(root, 0, args.depth, args.patterns)
    return result("tree", True, method="uia_tree", window=_brief(win), tree=tree,
                  cursor_before=before, cursor_after=get_cursor())


def _brief(win):
    return {k: win[k] for k in ("title", "process", "pid", "hwnd", "monitor")}


def cmd_click(args):
    before = get_cursor()
    win, _ = resolve_window(args.window)
    ctl = find_control(root_control(win), args.selector, args.depth)
    pats = supported_patterns(ctl)
    method = None
    # 1) UIA patterns: no cursor involvement
    for name, action in (("Invoke", lambda p: p.Invoke()),
                         ("Toggle", lambda p: p.Toggle()),
                         ("SelectionItem", lambda p: p.Select())):
        if name in pats:
            action(getattr(ctl, f"Get{name}Pattern")())
            method = f"uia_{name}Pattern"
            break
    if method is None and "LegacyIAccessible" in pats:
        try:
            ctl.GetLegacyIAccessiblePattern().DoDefaultAction()
            method = "uia_LegacyIAccessible_DoDefaultAction"  # returns ok even if the app ignores it; verify with 'read'
        except Exception:
            pass
    if method:
        return result("click", True, method=method, cursor_before=before,
                      cursor_after=get_cursor(), window=_brief(win),
                      control={"name": ctl.Name, "id": ctl.AutomationId}, patterns=pats)
    # 2) real click, only if explicitly allowed
    if not args.allow_real_input:
        return result("click", False, method=None, cursor_before=before, cursor_after=get_cursor(),
                      error="no_uia_method",
                      detail="control supports no click-like UIA pattern; nothing was done. "
                             "Re-run with --allow-real-input to use a real mouse click (moves the cursor, "
                             "then restores it).",
                      window=_brief(win), control={"name": ctl.Name, "id": ctl.AutomationId},
                      patterns=pats, fallback_available="real_mouse_click")
    ctl.Click(simulateMove=False, waitTime=0.1)
    mid = get_cursor()
    set_cursor(*before)
    return result("click", True, method="real_mouse_click", real_input_used=True,
                  cursor_before=before, cursor_after=get_cursor(), cursor_during=list(mid),
                  cursor_restored=get_cursor() == before, window=_brief(win),
                  control={"name": ctl.Name, "id": ctl.AutomationId}, patterns=pats)


def _read_text(ctl, pats):
    """Return (text, method) or (None, None)."""
    if "Value" in pats:
        try:
            v = ctl.GetValuePattern().Value
            if v is not None:
                return v, "uia_ValuePattern"
        except Exception:
            pass
    if "Text" in pats:
        try:
            return ctl.GetTextPattern().DocumentRange.GetText(-1), "uia_TextPattern"
        except Exception:
            pass
    if "LegacyIAccessible" in pats:
        try:
            v = ctl.GetLegacyIAccessiblePattern().Value
            if v:
                return v, "uia_LegacyIAccessible_Value"
        except Exception:
            pass
    if ctl.Name:
        return ctl.Name, "uia_Name"
    return None, None


def cmd_read(args):
    before = get_cursor()
    win, _ = resolve_window(args.window)
    ctl = find_control(root_control(win), args.selector, args.depth)
    pats = supported_patterns(ctl)
    text, method = _read_text(ctl, pats)
    if method is None:
        return result("read", False, cursor_before=before, cursor_after=get_cursor(),
                      error="no_uia_method", detail="control exposes no readable text", patterns=pats)
    return result("read", True, method=method, text=text, cursor_before=before,
                  cursor_after=get_cursor(), window=_brief(win),
                  control={"name": ctl.Name, "id": ctl.AutomationId}, patterns=pats)


def _native_hwnd(ctl):
    try:
        return ctl.NativeWindowHandle or 0
    except Exception:
        return 0


def cmd_type(args):
    before = get_cursor()
    win, _ = resolve_window(args.window)
    ctl = find_control(root_control(win), args.selector, args.depth)
    pats = supported_patterns(ctl)
    text = args.text
    base = dict(window=_brief(win), control={"name": ctl.Name, "id": ctl.AutomationId}, patterns=pats)

    # 1) ValuePattern (no focus, no cursor). Skip if read-only.
    if "Value" in pats:
        vp = ctl.GetValuePattern()
        try:
            read_only = bool(vp.IsReadOnly)
        except Exception:
            read_only = False
        if not read_only:
            new = (vp.Value + text) if args.append else text
            vp.SetValue(new)
            return result("type", True, method="uia_ValuePattern", cursor_before=before,
                          cursor_after=get_cursor(), appended=args.append, **base)
    # 2) WM_SETTEXT / WM_CHAR to the control's own HWND (no cursor, no focus change)
    h = _native_hwnd(ctl)
    if h:
        if args.append:
            for ch in text:
                user32.SendMessageW(h, 0x0102, ord(ch), 0)  # WM_CHAR
            method = "window_message_WM_CHAR"
        else:
            user32.SendMessageW(h, 0x000C, 0, ctypes.c_wchar_p(text))  # WM_SETTEXT
            method = "window_message_WM_SETTEXT"
        return result("type", True, method=method, cursor_before=before, cursor_after=get_cursor(),
                      appended=args.append, **base)
    # 3) real keyboard (needs foreground focus), only if allowed
    if not args.allow_real_input:
        return result("type", False, cursor_before=before, cursor_after=get_cursor(),
                      error="no_uia_method",
                      detail="control has no writable ValuePattern and no HWND for window messages; "
                             "nothing was typed. --allow-real-input uses real keystrokes (steals "
                             "keyboard focus; mouse is not moved).",
                      fallback_available="real_keyboard", **base)
    ctl.SetFocus()
    if not args.append:
        ctl.SendKeys("{Ctrl}a{Del}", waitTime=0.05)
    ctl.SendKeys(text, interval=0.005, waitTime=0.05)
    return result("type", True, method="real_keyboard", real_input_used=True,
                  cursor_before=before, cursor_after=get_cursor(), appended=args.append, **base)


def cmd_focus(args):
    before = get_cursor()
    win, _ = resolve_window(args.window)
    if win["minimised"]:
        user32.ShowWindow(win["hwnd"], 9)  # SW_RESTORE
    root = root_control(win)
    method = "uia_SetFocus"
    ok = False
    try:
        if args.selector:
            find_control(root, args.selector, args.depth).SetFocus()
            ok = True
        else:
            root.SetFocus()
            ok = True
    except WinctlError:
        raise
    except Exception:
        pass
    if not ok or not args.selector:
        # SetForegroundWindow: changes keyboard focus/z-order, never the cursor
        user32.SetForegroundWindow(win["hwnd"])
        method = "SetForegroundWindow" if not ok else "uia_SetFocus+SetForegroundWindow"
    time.sleep(0.15)
    fg = int(user32.GetForegroundWindow())
    return result("focus", True, method=method, cursor_before=before, cursor_after=get_cursor(),
                  window=_brief(win), is_foreground=(fg == win["hwnd"]))


def _mss():
    import mss
    return (getattr(mss, "MSS", None) or mss.mss)()


def capture_window_image(hwnd):
    """Return (PIL image, method). PrintWindow(PW_RENDERFULLCONTENT) captures only that window,
    even when occluded; falls back to a screen-region grab (may include overlapping windows)."""
    import win32gui, win32ui
    from PIL import Image
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise WinctlError("capture_failed", "window has zero size (minimised?)")
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    save = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    save.SelectObject(bmp)
    ok = user32.PrintWindow(hwnd, save.GetSafeHdc(), 2)
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    save.DeleteDC(); mfc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc)
    if ok and img.getbbox() is not None:
        return img, "PrintWindow_PW_RENDERFULLCONTENT"
    with _mss() as sct:
        shot = sct.grab({"left": r.left, "top": r.top, "width": w, "height": h})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), "screen_region_grab(mss)"


def cmd_screenshot_window(args):
    before = get_cursor()
    win, _ = resolve_window(args.window)
    if win["minimised"]:
        raise WinctlError("capture_failed", "window is minimised; run 'focus' first")
    img, method = capture_window_image(win["hwnd"])
    img.save(args.outfile)
    mons = list_monitors()
    mon = next((m for m in mons if m["number"] == win["monitor"]), None)
    return result("screenshot-window", True, method=method, cursor_before=before,
                  cursor_after=get_cursor(), window=_brief(win), outfile=args.outfile,
                  size=list(img.size), monitor_scale=mon["scale"] if mon else None,
                  note=("may include windows overlapping this one" if method.startswith("screen") else
                        "captures only this window, even if partly covered"))


def cmd_screenshot_monitor(args):
    before = get_cursor()
    mons = list_monitors()
    mon = next((m for m in mons if m["number"] == args.monitor), None)
    if mon is None:
        raise WinctlError("monitor_not_found", f"monitor {args.monitor} not in 1..{len(mons)}")
    from PIL import Image
    l, t, r, b = mon["rect"]
    with _mss() as sct:
        shot = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    img.save(args.outfile)
    return result("screenshot-monitor", True, method="mss_region_grab", cursor_before=before,
                  cursor_after=get_cursor(), monitor=mon, outfile=args.outfile, size=list(img.size))


# =============================================================================
def build_parser():
    p = argparse.ArgumentParser(prog="winctl", description="Native Windows app control via UI Automation (JSON out).")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, *positional, real=False, depth=True):
        sp = sub.add_parser(name)
        for a in positional:
            sp.add_argument(a)
        if real:
            sp.add_argument("--allow-real-input", action="store_true",
                            help="permit a real mouse/keyboard fallback (reported, never silent)")
        if depth:
            sp.add_argument("--depth", type=int, default=14, help="max control search depth")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("list-windows", cmd_list_windows, depth=False)
    sp.add_argument("--all", action="store_true", help="include untitled windows")
    sp = add("tree", cmd_tree, "window", depth=False)
    sp.add_argument("--depth", type=int, default=3)
    sp.add_argument("--patterns", action="store_true", help="include supported UIA patterns per node (slower)")
    add("click", cmd_click, "window", "selector", real=True)
    sp = add("type", cmd_type, "window", "selector", "text", real=True)
    sp.add_argument("--append", action="store_true", help="append instead of replace")
    add("read", cmd_read, "window", "selector")
    sp = add("focus", cmd_focus, "window")
    sp.add_argument("selector", nargs="?", default=None)
    add("screenshot-window", cmd_screenshot_window, "window", "outfile", depth=False)
    sp = add("screenshot-monitor", cmd_screenshot_monitor, depth=False)
    sp.add_argument("monitor", type=int)
    sp.add_argument("outfile")
    return p


def run(argv):
    args = build_parser().parse_args(argv)
    before = get_cursor()
    try:
        with contextlib.redirect_stdout(sys.stderr):  # keep stdout pure JSON
            res = args.fn(args)
    except WinctlError as e:
        res = result(args.command, False, cursor_before=before, cursor_after=get_cursor(),
                     error=e.code, detail=e.detail)
    except Exception as e:  # unexpected: still report cleanly
        res = result(args.command, False, cursor_before=before, cursor_after=get_cursor(),
                     error="unexpected", detail=f"{type(e).__name__}: {e}")
    return res


def main(argv=None):
    res = run(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
