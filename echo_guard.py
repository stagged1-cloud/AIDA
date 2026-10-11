"""Stops the assistant treating its own voice as the user's.

Two independent layers (no I/O, no framework imports, unit-tested in test_echo_gate.py):

  EchoGate   half-duplex gate. While AIDA is speaking, plus a short tail afterwards, the microphone
             stream is blanked (zeros, so timing is kept) instead of reaching speech recognition.
             The hotkey stays authoritative: barge_in() opens the gate for a moment so the user can talk over it.
  SpokenLog  rolling record of what AIDA said recently. clean() strips any run of words from a transcript
             that matches that speech, keeping whatever the user actually said.
"""
import re
import threading
import time
from difflib import SequenceMatcher

# Words that must always get through when they are the whole point of the utterance.
COMMAND_WORDS = {"confirmed", "confirm", "stop", "yes", "yeah", "no", "cancel", "repeat", "okay", "ok", "go",
                 "shut", "quiet", "restart", "commit", "push", "sure", "nope", "right"}

# Filler glued onto the edge of a removed echo ("so", "and"...) is dropped with it.
EDGE_FILLER = {"so", "and", "um", "uh", "the", "a", "but", "well", "oh", "then", "like"}


def norm_words(text):
    """Lower-cased word list, punctuation and apostrophes stripped (i'd -> id)."""
    return re.findall(r"[^\W_]+", (text or "").lower().replace("'", "").replace("’", ""))


class EchoGate:
    """True from blocked() while AIDA is speaking and for tail_s afterwards (unless barging in)."""

    def __init__(self, speaking, tail_s=0.6, enabled=True, clock=time.monotonic):
        self.speaking, self.tail_s, self.enabled, self.clock = speaking, tail_s, enabled, clock
        self.last_speaking = -1e9
        self.bypass_until = -1e9
        self.last_audible = -1e9             # last time AIDA was seen speaking (barge-in does not reset this)

    def barge_in(self, hold_s=0.8):
        """You pressed the talk hotkey: ignore the gate for hold_s (AIDA is being stopped right now)."""
        self.bypass_until = self.clock() + hold_s
        self.last_speaking = -1e9

    def echo_possible(self, capture_s, margin_s=1.5):
        """True if AIDA was audible during the last capture_s seconds (+ margin for room echo / speaker tail).
        Only then can a transcript contain his own voice; otherwise the text filter must not touch the user's words
        (it was deleting the user quoting AIDA back, e.g. 11 Oct 00:12:08)."""
        now = self.clock()
        if self.speaking():
            self.last_audible = now
        return now - self.last_audible <= float(capture_s) + float(margin_s)

    def blocked(self):
        if self.speaking():                  # tracked even when the gate is off or bypassed (echo_possible)
            self.last_audible = self.clock()
        if not self.enabled:
            return False
        now = self.clock()
        if now < self.bypass_until:
            return False
        if self.speaking():
            self.last_speaking = now
            return True
        return now - self.last_speaking < self.tail_s


class SpokenLog:
    """What AIDA has said lately, and the filter that removes it from a transcript."""

    def __init__(self, window_s=90.0, min_block=4, clock=time.time):
        self.window_s, self.min_block, self.clock = window_s, min_block, clock
        self.items = []                      # (time, [words])
        self.lock = threading.Lock()

    def add(self, text):
        w = norm_words(text)
        if not w:
            return
        with self.lock:
            now = self.clock()
            self.items = [(t, x) for t, x in self.items if now - t <= self.window_s] + [(now, w)]

    def recent(self):
        now = self.clock()
        with self.lock:
            return [x for t, x in self.items if now - t <= self.window_s]

    def clean(self, text):
        """(cleaned_text, removed_any). Removes runs of >= min_block words that AIDA recently said."""
        tokens = (text or "").split()
        words = [norm_words(t) for t in tokens]           # per token (a token may normalise to 0..n words)
        flat = [(i, w) for i, ws in enumerate(words) for w in ws]
        if len(flat) < self.min_block:
            return text, False                              # short utterances (confirmed, stop, yes...) always pass
        spoken = self.recent()
        if not spoken:
            return text, False
        if len(flat) <= 4 and flat[0][1] in COMMAND_WORDS:
            return text, False
        tw = [w for _, w in flat]
        drop = [False] * len(tw)
        for sp in spoken:
            sm = SequenceMatcher(None, tw, sp, autojunk=False)
            for a, b, size in sm.get_matching_blocks():
                if size >= self.min_block:
                    for k in range(a, a + size):
                        drop[k] = True
        if not any(drop):
            return text, False
        # bridge a single unmatched word between two dropped runs (recogniser slips inside an echo)
        for k in range(1, len(drop) - 1):
            if not drop[k] and drop[k - 1] and drop[k + 1]:
                drop[k] = True
        # take the filler word sitting on the edge of a dropped run with it
        for k in range(len(drop)):
            if not drop[k] and tw[k] in EDGE_FILLER:
                if (k > 0 and drop[k - 1]) or (k + 1 < len(drop) and drop[k + 1]):
                    drop[k] = True
        # a token is kept only if none of its words were dropped
        gone = set()
        for (i, _), d in zip(flat, drop):
            if d:
                gone.add(i)
        kept = [t for i, t in enumerate(tokens) if i not in gone and words[i]]
        out = " ".join(kept).strip(" ,.;:-")
        return out, True
