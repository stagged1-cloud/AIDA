"""Hold spoken results while the user is talking (11 Oct 2026).

Why: anything AIDA says while the user is mid-speech trips the echo gate, which blanks the push-to-talk mic
(his words become silence) and aborts an open-mic capture outright. So a reply or helper result that
arrives while he is talking is parked here and released, in order, once he has finished (talk key
pressed again / open-mic turn ended) or after max_hold_s as a safety net. Nothing is ever dropped.

SpeechHold is pure apart from one daemon thread that polls the hold condition; tests drive release()
directly with a fake clock.
"""
import threading
import time


class SpeechHold:
    def __init__(self, put, is_talking, max_hold_s=180.0, poll_s=0.1, clock=time.monotonic, log=None):
        """put(text): hands a line to the real speech queue. is_talking(): True while the user is speaking."""
        self.put, self.is_talking = put, is_talking
        self.max_hold_s, self.poll_s, self.clock = float(max_hold_s), float(poll_s), clock
        self.log = log or (lambda who, text: None)
        self.held = []                 # lines waiting, oldest first
        self.since = None              # clock() when the oldest held line arrived
        self.lock = threading.Lock()
        self._thread = None

    def _talking(self):
        try:
            return bool(self.is_talking())
        except Exception:
            return False               # a broken check must never swallow speech

    def offer(self, text, start_thread=True):
        """Speak now, or hold if he is talking. Anything already held goes first, so order is kept.
        Returns True if the line was held."""
        with self.lock:
            if not self.held and not self._talking():
                self.put(text)
                return False
            if not self.held:
                self.since = self.clock()
                self.log("system", "Holding my reply until you've finished talking.")
            self.held.append(text)
            new = None
            if start_thread and self._thread is None:
                new = self._thread = threading.Thread(target=self._watch, name="speech-hold", daemon=True)
        if new is not None:
            new.start()
        return True

    def release(self, force=False):
        """Pass held lines to the speech queue if he has stopped talking (or the hold timed out).
        Returns the number released."""
        with self.lock:
            if not self.held:
                return 0
            timed_out = self.since is not None and self.clock() - self.since >= self.max_hold_s
            if not force and not timed_out and self._talking():
                return 0
            lines, self.held, self.since = self.held, [], None
            for t in lines:
                self.put(t)
            return len(lines)

    def _watch(self):
        while True:
            time.sleep(self.poll_s)
            self.release()
            with self.lock:
                if not self.held:
                    self._thread = None        # cleared under the lock, so offer() starts a fresh watcher
                    return
