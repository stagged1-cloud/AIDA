"""The 'waiting for your "confirmed"' state, as a small clock-driven object (no Tk, no I/O).

It used to be a bare flag that only cleared when the user spoke again, so a stale block (or any reply that merely
contained the word 'confirm') kept the header on 'Waiting for you'. Now it:
  * clears the moment a confirmation is recorded,
  * clears when the job ends or a new job starts (the reply may then re-arm it, if it really asks for 'confirmed'),
  * expires on its own after WINDOW_S (the same 3 minutes as the safety hook's token),
  * remembers WHAT is blocked so the banner can say so.
"""
import re
import time

WINDOW_S = 180

_BLOCK_RE = re.compile(r"BLOCKED until the user confirms by voice \((.+?)\)\.\s", re.S)
# a reply that genuinely asks for the word, not one that merely contains "confirmed"
_ASK_RE = re.compile(
    r"(?:\b(?:say|type|reply|tell me|give me)\b\W+(?:\w+\W+){0,3}?confirmed\b"
    r"|\bwaiting (?:for|on) (?:your |a |the )?confirm"
    r"|\bneed(?:s)? (?:your |a )?confirm"
    r"|\bplease confirm\b|\bask(?:ing)? you to confirm\b|\bconfirm (?:it|that|this|to go ahead)\b)", re.I)


# --- speech-to-text mishearings of the safety word -------------------------------------------------------------
# Edit this one list to add/remove accepted mishearings. Exact word match only: no fuzzy or substring matching.
CONFIRM_WORD_VARIANTS = ("confirmed", "confirm", "confined", "confound", "concerned", "conformed", "confirmation")
# Rule (FIRST word of the user's own utterance only, case-insensitive, leading quotes/space ignored). A variant counts when:
#   (a) it is the whole utterance ("Confound."), or
#   (b) it is followed straight away by punctuation ("Concerned, skip it"), or
#   (c) its next word is a continuation word below ("confirmed and then...", "confirm do it").
# So "Concerned about the budget..." / "confirmation email" / "I confirm..." do NOT count.
CONFIRM_CONTINUERS = ("and", "then", "now", "do", "go", "please", "so", "yes", "yeah", "ok", "okay", "it")
_VARIANT_RE = re.compile(r"^[\s\"'“‘]*(" + "|".join(CONFIRM_WORD_VARIANTS) + r")(?![A-Za-z])(.*)$", re.I | re.S)


def is_confirm_variant(text):
    """True if `text` (the user's own speech or typed message, never helper/tool output) opens with a heard-as-confirmed word."""
    if not isinstance(text, str) or not text.strip():
        return False
    m = _VARIANT_RE.match(text)
    if not m:
        return False
    rest = m.group(2)
    if not rest.strip():
        return True
    if rest[:1] in ",.;:!?-–—…\"'”’)":
        return True
    nxt = re.match(r"\s+([A-Za-z]+)", rest)
    return bool(nxt and nxt.group(1).lower() in CONFIRM_CONTINUERS)


def clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def reply_question(reply):
    """The last question AIDA asked (clipped to its first words), or None. An ordinary 'your turn', not a block."""
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", reply or "") if x.strip()]
    if sents and sents[-1].endswith("?"):
        return clip(sents[-1], 60)
    return None


def describe_block(body):
    """Short 'what is blocked' from the safety hook's message, or '' if it gave none. 'command: git push' -> 'git push'."""
    m = _BLOCK_RE.search((body or "").replace('\\"', '"'))
    if not m:
        return ""
    what = re.sub(r"^(command|tool):\s*", "", m.group(1).strip())
    return clip(what, 70)


def reply_asks_confirm(reply):
    """The sentence of `reply` that asks the user to say 'confirmed' (clipped), or None."""
    for sent in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
        if _ASK_RE.search(sent):
            return clip(sent.strip(), 70)
    return None


class ConfirmState:
    def __init__(self, clock=time.time, window_s=WINDOW_S):
        self.clock, self.window_s = clock, window_s
        self.until = 0.0
        self.what = ""
        self.kind = "confirm"               # "confirm" = the safety check wants the word; "ask" = an ordinary question

    @property
    def active(self):
        return self.clock() < self.until

    def arm(self, what=""):
        self.until = self.clock() + self.window_s
        self.what = what or ""
        self.kind = "confirm"

    def ask(self, question):
        """AIDA asked an ordinary question: neutral 'Your turn', no mention of 'confirmed'."""
        self.until = self.clock() + self.window_s
        self.what = question or ""
        self.kind = "ask"

    def clear(self):
        self.until = 0.0
        self.what = ""
        self.kind = "confirm"

    def banner(self):
        """(state, detail) for the status line while genuinely waiting, else None."""
        if not self.active:
            return None
        if self.kind == "ask":
            return "Waiting", self.what or "Your turn"
        if self.what:
            return "Confirm", f'Waiting for your "confirmed": {self.what}'
        return "Confirm", 'Waiting for your "confirmed" (the safety check blocked something)'
