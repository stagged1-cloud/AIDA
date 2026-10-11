"""Push-to-talk / cancel key bindings: parsing, validation, live rebinding. No Tk, no global hooks at import time.

    * normalise(spec)            'Control+Alt+T' -> 'ctrl+alt+t' (canonical order: ctrl, alt, shift, windows, key)
    * validate(spec, which, ...) -> Verdict(level 'ok'|'warn'|'error', message). 'warn' can be overridden by the user.
    * event_to_spec(...)         Tk key event -> spec string (used by the capture dialog)
    * HotkeyManager              owns the registrations. Rebinding unregisters the old key, registers the new one and
                                 rolls back to the old one if registering fails. The backend is injectable, so tests
                                 use a fake; production uses KeyboardBackend (the `keyboard` package voice_claude uses).
    * resolve(state, cfg)        saved choice in state.json wins over config.yaml (config defaults stay untouched).

Mouse buttons: NOT supported. The `keyboard` package only hooks the keyboard, and no mouse package is installed
(no new dependency was added for this).
"""
import collections

DEFAULTS = {"ptt": "`", "cancel": "ctrl+`"}
STATE_KEYS = {"ptt": "hotkey", "cancel": "cancel_hotkey"}       # state.json / config.yaml key names
LABELS = {"ptt": "Push-to-talk key", "cancel": "Cancel key"}

MODS = ("ctrl", "alt", "shift", "windows")
MOD_ALIASES = {"control": "ctrl", "ctl": "ctrl", "option": "alt", "menu": "alt", "win": "windows", "cmd": "windows",
               "super": "windows", "meta": "windows", "left windows": "windows", "right windows": "windows",
               "left ctrl": "ctrl", "right ctrl": "ctrl", "left alt": "alt", "right alt": "alt",
               "left shift": "shift", "right shift": "shift"}
KEY_ALIASES = {"return": "enter", "escape": "esc", "del": "delete", "ins": "insert", "pgup": "page up",
               "pageup": "page up", "prior": "page up", "pgdn": "page down", "pagedown": "page down",
               "next": "page down", "backtick": "`", "grave": "`", "spacebar": "space", "bksp": "backspace",
               "back": "backspace", "caps": "caps lock", "capslock": "caps lock", "prtsc": "print screen",
               "printscreen": "print screen", "prtscn": "print screen", "scrolllock": "scroll lock",
               "break": "pause", "plus": "+", "minus": "-", "comma": ",", "period": ".", "dot": "."}
NAMED_KEYS = {"space", "enter", "esc", "tab", "backspace", "delete", "insert", "home", "end", "page up", "page down",
              "up", "down", "left", "right", "caps lock", "print screen", "scroll lock", "pause", "num lock",
              "apps"} | {f"f{i}" for i in range(1, 25)}
NON_PRINTING = {"f%d" % i for i in range(1, 25)} | {"insert", "home", "end", "page up", "page down", "print screen",
                                                      "scroll lock", "pause", "num lock", "apps", "caps lock"}
# non-printing but used for typing/navigating in the text box: allowed only after an explicit "Use anyway"
EDITING_KEYS = {"enter", "tab", "backspace", "delete", "up", "down", "left", "right"}
PRINTABLE_PUNCT = set("`-=[];',./\\#+")
# Combos the OS owns, or that the app / text box needs (the hotkey is global and suppressed, so binding these would
# break copy and paste, task switching, etc.)
RESERVED = {"ctrl+alt+delete", "ctrl+shift+esc", "ctrl+esc", "alt+tab", "alt+f4", "alt+esc", "alt+space",
            "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "ctrl+y", "shift+insert", "ctrl+insert",
            "shift+delete", "alt+enter", "ctrl+w", "ctrl+f4"}

Verdict = collections.namedtuple("Verdict", "level message")


def display(spec):
    """'ctrl+alt+t' -> 'Ctrl+Alt+T'; '`' -> '`'; 'f9' -> 'F9'. Empty -> 'not set'."""
    if not spec:
        return "not set"
    out = []
    for p in split(spec):
        if p in MODS:
            out.append({"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "windows": "Win"}[p])
        elif len(p) == 1:
            out.append(p.upper())
        else:
            out.append(p.title() if not (p.startswith("f") and p[1:].isdigit()) else p.upper())
    return "+".join(out)


def split(spec):
    """Tokens of a spec, handling a literal '+' key ('ctrl++')."""
    s = str(spec or "").strip().lower()
    if not s:
        return []
    if s == "+":
        return ["+"]
    parts = [p.strip() for p in s.split("+")]
    if s.endswith("+") and len(parts) >= 2 and parts[-1] == "" and parts[-2] == "":
        parts = parts[:-2] + ["+"]
    return [p for p in parts if p != ""]


def parse(spec):
    """-> (frozenset of modifiers, key) or raises ValueError with a readable message."""
    toks = [MOD_ALIASES.get(t, t) for t in split(spec)]
    if not toks:
        raise ValueError("No key set.")
    mods = [t for t in toks if t in MODS]
    keys = [KEY_ALIASES.get(t, t) for t in toks if t not in MODS]
    if len(keys) != 1:
        raise ValueError("A hotkey is any modifiers plus exactly one key." if keys else
                         "A modifier on its own can't be a hotkey; add a key.")
    key = keys[0]
    if len(set(mods)) != len(mods):
        raise ValueError("Repeated modifier.")
    if not (len(key) == 1 and (key.isalnum() or key in PRINTABLE_PUNCT)) and key not in NAMED_KEYS:
        raise ValueError(f"Don't recognise the key '{key}'.")
    return frozenset(mods), key


def normalise(spec):
    """Canonical lowercase form. '' stays ''. Raises ValueError for rubbish."""
    if not str(spec or "").strip():
        return ""
    mods, key = parse(spec)
    return "+".join([m for m in MODS if m in mods] + [key])


def is_printable(key):
    return key == "space" or (len(key) == 1 and (key.isalnum() or key in PRINTABLE_PUNCT))


def validate(spec, which="ptt", other="", defaults=DEFAULTS):
    """Check a candidate binding. `other` = the binding of the other action (duplicates are refused).
    The shipped defaults are always accepted (the default push-to-talk key is a plain backtick)."""
    try:
        n = normalise(spec)
    except ValueError as e:
        return Verdict("error", str(e))
    if not n:
        if which == "ptt":
            return Verdict("error", "Push-to-talk needs a key. (Clear is only for the cancel key.)")
        return Verdict("ok", "Cancel key will be switched off.")
    try:
        o = normalise(other)
    except ValueError:
        o = ""
    if n == o:
        return Verdict("error", f"{display(n)} is already used for the "
                                f"{'cancel' if which == 'ptt' else 'push-to-talk'} key.")
    if n == normalise(defaults[which]):
        return Verdict("ok", "Default key.")
    mods, key = parse(n)
    if "windows" in mods:
        return Verdict("error", "Windows-key combinations are reserved by Windows.")
    if n in RESERVED:
        return Verdict("error", f"{display(n)} is reserved by Windows or needed for typing, copy and paste.")
    plain = not (mods - {"shift"})                       # no ctrl/alt: typing a character would trigger it
    if plain and is_printable(key):
        return Verdict("error", "That key types a character, and the hotkey is global, so it would fire while you "
                                "type in the box. Use a function key, or add Ctrl or Alt.")
    if plain and key in EDITING_KEYS:
        return Verdict("warn", f"{display(n)} is used for typing and navigating, so it would fire while you edit text.")
    if which == "ptt" and n == "esc":
        return Verdict("warn", "Esc is used to close dialogs and cancel things in other apps.")
    if {"ctrl", "alt"} <= mods and is_printable(key):
        return Verdict("warn", "Ctrl+Alt is AltGr on some layouts and can type characters, so this may misfire.")
    return Verdict("ok", "")


# ------------------------------------------------------------------ Tk key event -> spec
_KEYSYM_NAMES = {"grave": "`", "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
                 "semicolon": ";", "apostrophe": "'", "comma": ",", "period": ".", "slash": "/",
                 "backslash": "\\", "numbersign": "#", "space": "space", "return": "enter", "tab": "tab",
                 "backspace": "backspace", "delete": "delete", "insert": "insert", "home": "home", "end": "end",
                 "prior": "page up", "next": "page down", "up": "up", "down": "down", "left": "left",
                 "right": "right", "pause": "pause", "scroll_lock": "scroll lock", "num_lock": "num lock",
                 "caps_lock": "caps lock", "print": "print screen", "menu": "apps", "escape": "esc",
                 "kp_enter": "enter", "iso_left_tab": "tab"}
MOD_KEYSYMS = {"shift_l", "shift_r", "control_l", "control_r", "alt_l", "alt_r", "super_l", "super_r", "meta_l",
               "meta_r", "caps_lock", "num_lock", "scroll_lock", "alt_gr", "iso_level3_shift", "win_l", "win_r"}
# Tk on Windows: event.state bits
ST_SHIFT, ST_CTRL, ST_ALT = 0x1, 0x4, 0x20000


def event_to_spec(keysym, state, win_down=False):
    """Tk <KeyPress> -> (spec or None, note). None with an empty note = a bare modifier was pressed (keep waiting).
    Letters/digits/F-keys are layout independent; other symbols come from the keysym (so UK layouts work)."""
    k = str(keysym or "")
    low = k.lower()
    if low in MOD_KEYSYMS and low not in ("caps_lock", "num_lock", "scroll_lock"):
        return None, ""
    mods = []
    if state & ST_CTRL:
        mods.append("ctrl")
    if state & ST_ALT:
        mods.append("alt")
    if state & ST_SHIFT:
        mods.append("shift")
    if win_down:
        mods.append("windows")
    if len(low) == 1 and low.isalnum() and low.isascii():
        key = low
    elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        key = low
    elif low in _KEYSYM_NAMES:
        key = _KEYSYM_NAMES[low]
    else:
        return None, f"Can't use that key ({k or 'unknown'}). Try it without Shift, or pick a different key."
    return normalise("+".join(mods + [key])), ""


class CaptureModel:
    """State behind the key-capture dialog (pure; the Tk dialog just renders it)."""

    def __init__(self, which, current, other):
        self.which, self.current, self.other = which, current or "", other or ""
        self.spec = None                  # captured candidate (normalised) or None
        self.note = ""
        self.verdict = Verdict("ok", "")
        self.override = False
        self.cleared = False

    def key(self, keysym, state, win_down=False):
        if win_down:
            self.spec, self.cleared, self.override = None, False, False
            self.verdict = Verdict("error", "Windows-key combinations are reserved by Windows.")
            return
        spec, note = event_to_spec(keysym, state, win_down)
        if spec is None:
            if note:
                self.spec, self.verdict, self.cleared, self.override = None, Verdict("error", note), False, False
            return
        self.spec, self.cleared, self.override = spec, False, False
        self.verdict = validate(spec, self.which, self.other)

    def clear(self):
        self.spec, self.cleared, self.override = "", True, False
        self.verdict = validate("", self.which, self.other)

    def use_anyway(self):
        if self.verdict.level == "warn":
            self.override = True

    @property
    def can_save(self):
        if self.spec is None:
            return False
        if self.verdict.level == "error":
            return False
        return self.verdict.level == "ok" or self.override

    @property
    def needs_override(self):
        return self.spec is not None and self.verdict.level == "warn" and not self.override

    def result(self):
        return self.spec if self.can_save else None


# ------------------------------------------------------------------ live registration
class KeyboardBackend:
    """The real thing: the `keyboard` package. add() returns a handle; remove(handle) unregisters it."""

    def add(self, spec, callback):
        import keyboard
        return keyboard.add_hotkey(spec, callback, suppress=True)

    def remove(self, handle):
        import keyboard
        keyboard.remove_hotkey(handle)


class HotkeyManager:
    """Owns the live registrations for 'ptt' and 'cancel'.
    callbacks: {'ptt': fn, 'cancel': fn}. initial: {'ptt': spec, 'cancel': spec} ('' = off)."""

    def __init__(self, backend, callbacks, initial, log=None):
        self.backend, self.callbacks = backend, dict(callbacks)
        self.log = log or (lambda *a: None)
        self.bound = {"ptt": "", "cancel": ""}
        self.handles = {}
        for name in ("ptt", "cancel"):
            self._add(name, normalise(initial.get(name, "")))

    def _add(self, name, spec):
        if spec:
            self.handles[name] = self.backend.add(spec, self.callbacks[name])
        self.bound[name] = spec

    def _remove(self, name):
        h = self.handles.pop(name, None)
        if h is not None:
            self.backend.remove(h)
        self.bound[name] = ""

    def other(self, name):
        return self.bound["cancel" if name == "ptt" else "ptt"]

    def rebind(self, name, spec):
        """Unregister the old key, register the new one. On failure the old key is restored and the error re-raised
        as RuntimeError. Returns the normalised spec now live."""
        spec = normalise(spec)
        old = self.bound[name]
        self._remove(name)
        try:
            self._add(name, spec)
        except Exception as e:
            self.bound[name] = ""
            try:
                self._add(name, old)
            except Exception as e2:
                self.log("error", f"Could not restore {LABELS[name]} {display(old)}: {e2}")
                raise RuntimeError(f"Could not register {display(spec)} ({e}), and could not restore "
                                   f"{display(old)} ({e2}).") from e
            raise RuntimeError(f"Could not register {display(spec)}: {e}. Kept {display(old)}.") from e
        return spec


def resolve(state, cfg, defaults=DEFAULTS):
    """Effective bindings: saved choice in state.json wins over config.yaml, which wins over the built-in default.
    An empty saved cancel key means 'switched off'. A garbled saved value is ignored."""
    out = {}
    for name, k in STATE_KEYS.items():
        val = None
        if k in state:
            try:
                val = normalise(state[k])
            except ValueError:
                val = None
            if val == "" and name == "ptt":
                val = None
        if val is None:
            try:
                val = normalise(cfg.get(k, defaults[name]))
            except ValueError:
                val = normalise(defaults[name])
        out[name] = val
    return out
