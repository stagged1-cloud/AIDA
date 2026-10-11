"""
Windowless (no console) support for the voice app.

When start.bat runs the app with pythonw.exe there is no console: sys.stdout / sys.stderr are None, so every
print() would crash or vanish. This module (stdlib + optional yaml, no other imports) provides:

  setup(log_dir)            point None stdout/stderr at a rotating logs\\console.txt, return True if it did
  install_excepthook(...)   uncaught fatal error -> traceback in console.txt + a Windows message box
  chat_window_enabled(cfg)  used by start.bat (`python windowless.py check`, exit 0 = go windowless)
  QuitGate                  "quit" voice/typed command that needs a spoken/typed "confirmed" before it acts
"""
import os
import re
import sys
import time
import traceback

MAX_BYTES = 1_000_000
KEEP = 3                      # console.txt, console.txt.1, console.txt.2
TITLE = "claude2claude voice"


class RotatingFile:
    """Minimal file-like object for sys.stdout / sys.stderr. Line-buffered, rotates at MAX_BYTES, never raises."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path, max_bytes=MAX_BYTES, keep=KEEP):
        self.path, self.max_bytes, self.keep = str(path), int(max_bytes), int(keep)
        self.f = None
        self._open()

    def _open(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self.f = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            self.f = None

    def _rotate(self):
        try:
            self.f.close()
            for i in range(self.keep - 1, 0, -1):
                src = self.path if i == 1 else f"{self.path}.{i - 1}"
                if os.path.exists(src):
                    os.replace(src, f"{self.path}.{i}")
        except OSError:
            pass
        self._open()

    def write(self, s):
        if not s:
            return 0
        try:
            if self.f is None:
                return len(s)
            self.f.write(s if isinstance(s, str) else str(s))
            if self.f.tell() > self.max_bytes:
                self._rotate()
        except (OSError, ValueError):
            pass
        return len(s)

    def flush(self):
        try:
            if self.f:
                self.f.flush()
        except (OSError, ValueError):
            pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def fileno(self):
        raise OSError("no file descriptor")


def setup(log_dir, mod=sys):
    """If stdout or stderr is None (pythonw), redirect the missing ones to log_dir\\console.txt.
    Returns True when the app is windowless. `mod` is the sys module (injectable for tests)."""
    if mod.stdout is not None and mod.stderr is not None:
        return False
    stream = RotatingFile(os.path.join(str(log_dir), "console.txt"))
    stream.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} (windowless, pid {os.getpid()}) ---\n")
    if mod.stdout is None:
        mod.stdout = stream
    if mod.stderr is None:
        mod.stderr = stream
    return True


def _default_msgbox(text, title):
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 | 0x1000)       # MB_ICONERROR | MB_TOPMOST


def show_fatal(exc_text, log_path, msgbox=None):
    """Tell the user a windowless start died, so it is never a silent failure. Returns the text shown."""
    last = [ln for ln in exc_text.strip().splitlines() if ln.strip()][-1:] or ["unknown error"]
    body = f"The voice app stopped.\n\n{last[0]}\n\nFull details: {log_path}"
    try:
        (msgbox or _default_msgbox)(body, TITLE)
    except Exception:
        pass
    return body


def install_excepthook(log_dir, windowless, msgbox=None):
    """Uncaught exception -> traceback to stderr (console.txt when windowless) and, if windowless, a message box."""
    log_path = os.path.join(str(log_dir), "console.txt")

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass
        if windowless and not issubclass(etype, (KeyboardInterrupt, SystemExit)):
            show_fatal(text, log_path, msgbox)
    sys.excepthook = hook
    return hook


# ------------------------------------------------------------------ start.bat helper
def chat_window_enabled(config_path):
    """True only when config.yaml explicitly has chat_window: true and the status box (overlay) is not switched
    off (the window is the only way to quit with no console). Missing key / unreadable / malformed -> False,
    which makes start.bat take the normal console path."""
    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            return False
        return cfg.get("chat_window") is True and cfg.get("overlay", True) is not False
    except Exception:
        return False


def main(argv):
    if len(argv) > 1 and argv[1] == "check":
        here = os.path.dirname(os.path.abspath(__file__))
        return 0 if chat_window_enabled(os.path.join(here, "config.yaml")) else 1
    print(__doc__)
    return 2


# ------------------------------------------------------------------ quitting
QUIT_RE = re.compile(r"^(ok |okay )?(quit( aida| the assistant| the app| the program| the voice app)?|"
                     r"(shut down|shutdown|turn off|power down)( the| this)?( voice)?"
                     r"( assistant| aida| app| program))$")
CONFIRM_RE = re.compile(r"^(yes )?(confirmed?|confirm it|go ahead|do it|yes do it|approved?)\b")
ASK_TTL = 60.0


def _norm(text):
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


class QuitGate:
    """Two-step quit for voice and typed commands. handle() returns:
       "ask"  -> the quit phrase was heard; say "confirmed" within ASK_TTL seconds to shut down
       "quit" -> confirmation arrived in time; shut down
       None   -> not ours (any other text cancels a pending quit)"""

    def __init__(self, ttl=ASK_TTL):
        self.ttl = ttl
        self.until = None

    def handle(self, text, now=None):
        now = time.time() if now is None else now
        t = _norm(text)
        if QUIT_RE.match(t):
            self.until = now + self.ttl
            return "ask"
        pending, self.until = self.until, None
        if pending is not None and now <= pending and CONFIRM_RE.match(t):
            return "quit"
        return None


if __name__ == "__main__":
    sys.exit(main(sys.argv))
