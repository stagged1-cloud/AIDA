"""Runtime on/off switch for the floating animated head.

Pure logic, no Tk and no GL here: the Overlay passes in callables that create and close the real face, so the state
machine can be tested with a fake. The last choice is kept in state.json under "face_enabled" (voice/button choice
wins over config.yaml, same approach as "watcher_enabled").
"""
import re
import threading

STATE_KEY = "face_enabled"

def parse_command(text):
    """'head off' / 'turn the head off' / 'head on' -> False / False / True. Anything else -> None."""
    t = re.sub(r"[^\w\s]", "", (text or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r" please$", "", t)
    m = re.fullmatch(r"(?:ok |okay |hey |please )?(?:can you |could you )?(?:turn |switch )?(?:the |your )?"
                     r"(?:(?:animated|floating|3d) )?(?:head|face) (on|off)", t)
    if m:
        return m.group(1) == "on"
    m = re.fullmatch(r"(?:ok |okay |please )?(?:can you |could you )?(?:turn |switch )(on|off) (?:the |your )?"
                     r"(?:(?:animated|floating|3d) )?(?:head|face)", t)
    if m:
        return m.group(1) == "on"
    m = re.fullmatch(r"(?:ok |okay |please )?(show|hide) (?:the |your )?(?:(?:animated|floating|3d) )?(?:head|face)", t)
    if m:
        return m.group(1) == "show"
    return None


def initial(state, cfg):
    """Start-up choice: the remembered toggle wins over config.yaml's face_enabled."""
    v = (state or {}).get(STATE_KEY)
    if isinstance(v, bool):
        return v
    return bool((cfg or {}).get(STATE_KEY, True))


def remember(state, on, save):
    state[STATE_KEY] = bool(on)
    save(state)


class HeadController:
    """State machine around the real face. make() returns a face object (or raises); close(face) shuts it down.
    All real work should happen on the Tk thread: use request()/toggle_later() from other threads."""

    def __init__(self, make, close, state, save, log=None, schedule=None, on_change=None):
        self.make, self.close = make, close
        self.state, self.save = state, save
        self.log = log or (lambda *a: None)
        self.schedule = schedule or (lambda fn: fn())     # Overlay passes root.after(0, fn)
        self.on_change = on_change or (lambda on: None)
        self.face = None
        self._busy = threading.Lock()

    @property
    def is_on(self):
        return self.face is not None

    def start(self, wanted):
        """Initial bring-up. Never raises."""
        if wanted:
            return self.set(True, remember_choice=False)
        self.on_change(False)
        return False

    def request(self, on):
        """Thread-safe: queue a change onto the UI thread."""
        self.schedule(lambda: self.set(on))

    def toggle_later(self):
        self.schedule(self.toggle)

    def toggle(self):
        return self.set(not self.is_on)

    def set(self, on, remember_choice=True):
        """Switch the head. Returns the resulting state. Never raises; a re-entrant call is ignored."""
        if not self._busy.acquire(blocking=False):
            return self.is_on
        try:
            if on and self.face is None:
                try:
                    self.face = self.make()
                    self.log("face", "head on")
                except Exception as e:
                    self.face = None
                    self.log("face", f"head stays off: could not start it ({e})")
                    self.on_change(False)
                    return False
            elif not on and self.face is not None:
                f, self.face = self.face, None
                try:
                    self.close(f)
                except Exception as e:
                    self.log("face", f"head off (shutdown reported: {e})")
                else:
                    self.log("face", "head off")
            if remember_choice:
                remember(self.state, self.is_on, self.save)
            self.on_change(self.is_on)
            return self.is_on
        finally:
            self._busy.release()

