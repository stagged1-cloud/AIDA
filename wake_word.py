"""Open mic with a "Hey AIDA" wake word (milestone M1 of the live-assistant plan). No Tk, no app imports.

Pieces, smallest first:
  match_wake(text, aliases)  pure: does a transcript start with "hey AIDA" (or a known mishearing)? -> (bool, rest)
  WakeDetector               interface: feed(16 kHz mono float32 chunk) -> WakeHit or None
    WhisperWakeDetector      fallback, no training: a cheap energy gate finds a speech burst, Silero VAD (bundled in
                             faster-whisper) confirms it is speech, then a short Whisper transcription of at most
                             ~1.5 s is fuzzy-matched. Only bursts 0.4 s or longer are ever transcribed.
    OnnxScoreDetector        primary engine once a trained "hey aida" model exists (livekit-wakeword / openWakeWord
                             format). Untested here: no model exists yet, see research/live_assistant/M1_notes.md.
  WakeMachine                pure state machine (clock passed in): off / idle / woken / capturing / followup.
  WakeListener               thread owning ONE mic stream; resamples to 16 kHz, feeds the machine, hands finished
                             utterances to the app. Audio lives only in RAM; nothing is written before or after wake.
  WakeLog                    logs/wake.jsonl: time, event, score, engine. Never audio, never text.

Safety rules kept here and in voice_claude.handle_audio:
  * while AIDA speaks (or the echo gate tail runs, or PTT is recording, or a job is running) the machine is "blocked":
    it drops any capture and hears nothing, so AIDA cannot wake itself, muted or not (muted speech still counts);
  * a false wake is silent (no "Didn't catch that");
  * wake-sourced speech can never write the 'confirmed' token (enforced in voice_claude.handle_audio).
"""
import difflib
import json
import queue
import re
import threading
import time

SR = 16000

DEFAULT_ALIASES = ["hey aida", "hey ada", "hey ida", "hey ayda", "hey aider", "hey eider", "hey ida's", "hey a.i.d.a",
                   "hi aida", "ok aida", "okay aida", "a aida", "hey aidan"]
_HEY = {"hey", "hi", "ok", "okay", "hay", "a", "eh", "hej"}
_AIDA = "aida"


def normalise(text):
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def _phon(word):
    """Rough non-rhotic folding so 'tahz', 'tarrs', 'taaz' all look like 'aida'."""
    w = re.sub(r"(.)\1+", r"\1", word).replace("z", "s")
    return w.replace("ah", "ar")


def match_wake(text, aliases=None, fuzzy=0.75, max_skip=1):
    """True if the transcript STARTS with the wake phrase (one filler word like "oh" may come first).
    Returns (matched, remainder) where remainder is what he said after the wake phrase.
    A bare "aida" or "stars are bright" never matches: a hey-word must come first."""
    words = normalise(text).split()
    al = [normalise(a).split() for a in (aliases if aliases is not None else DEFAULT_ALIASES)]
    for skip in range(0, max_skip + 1):
        w = words[skip:]
        for a in al:                                      # exact alias (covers "hey stars", "hey cars" if listed)
            if a and w[:len(a)] == a:
                return True, " ".join(w[len(a):])
        if len(w) >= 2 and w[0] in _HEY:                  # accent-tolerant: hey + something close to "aida"
            if difflib.SequenceMatcher(None, _phon(w[1]), _AIDA).ratio() >= fuzzy and w[1] not in ("stars", "cars", "mars",
                                                                                              "tears", "tara", "jars"):
                return True, " ".join(w[2:])
    return False, ""


def strip_wake_phrase(text, aliases=None):
    """'Hey AIDA, open Chrome' -> 'open Chrome' (original casing kept). Text without the phrase comes back as-is."""
    ok, rest = match_wake(text, aliases)
    if not ok:
        return text
    if not rest:
        return ""
    # keep the original wording of the remainder: cut the original after as many words as were consumed
    n_total = len(normalise(text).split())
    n_rest = len(rest.split())
    orig = (text or "").split()
    return " ".join(orig[len(orig) - n_rest:]) if n_rest <= len(orig) and n_total == len(orig) else rest


def confirm_allowed(source):
    """The spoken guard word only counts from the talk key ('ptt') or typed text, never from the open mic: a video,
    a call or someone in the room could otherwise approve a blocked delete/send."""
    return source in ("ptt", "typed")


# ---------------------------------------------------------------- patience: when has he finished talking?
DEFAULT_END_WORDS = ["over", "send it", "thats it", "that's it"]
DEFAULT_CANCEL_WORDS = ["cancel that", "never mind", "nevermind", "forget it"]
# A sentence that stops on one of these sounds unfinished ("open the um", "and then I want to"), so wait longer.
CONTINUATION_WORDS = {"um", "uh", "er", "erm", "hmm", "and", "so", "but", "because", "cos", "if", "the", "a", "an",
                      "to", "of", "or", "with", "for", "then", "like", "my", "your", "is", "was", "that", "which",
                      "when", "on", "in", "at", "from", "into", "i", "we", "you", "its", "it", "also", "just", "mean",
                      "umm", "uhh", "mm", "hm", "ah", "eh", "plus", "maybe"}
# Prompt for the pause-check transcription: Whisper normally deletes 'um'/'uh', which made 'record screen one with
# sound, and um, um' look finished. Seeding the prompt with fillers makes it keep them.
TAIL_PROMPT = "Hey AIDA, um, so, uh, and um, er, I was, like, hmm, and then, um,"


def _phrases(items):
    return [normalise(p) for p in (items or []) if normalise(p)]


# Whisper invents these on silence / room noise (YouTube-outro training data). Trailing ones are not his speech.
_HALLUCINATION_RE = re.compile(
    r"(?:\b(?:thanks|thank you)\s+(?:so much\s+)?for\s+(?:watching|tuning in|listening|viewing)\b"
    r"|\bplease\s+(?:like\s+and\s+)?subscribe\b|\b(?:like\s+and\s+)?subscribe\b|\bsubtitles?\s+by\b"
    r"|\bcaptions?\s+by\b|\btranscri(?:bed|ption)\s+by\b|\bamara\.org\b|\bhave\s+a\s+(?:good|great|nice)\s+"
    r"(?:night|day|one)\b|\buntil\s+(?:the\s+)?next\s+(?:video|time)\b|\bsee\s+you\s+(?:next\s+time|in\s+the\s+next)\b)",
    re.I)
_HALLUCINATION_SOLO = {"thank you", "thanks", "thank you very much", "bye", "bye bye", "goodbye", "you", "f sir",
                       "sir", "everyone", "the end", "music", "applause", "silence"}


def strip_hallucinations(text):
    """Pure. Drop trailing Whisper silence-hallucinations ('thanks for tuning in', 'have a good night, everyone',
    'until the next video', bare 'thank you'/'bye') from the end of a transcript. Real speech before them is kept."""
    raw = (text or "").strip()
    while raw:
        parts = re.split(r"(?<=[,.!?;:])\s+", raw)
        last = parts[-1]
        n = normalise(last)
        if not n or n in _HALLUCINATION_SOLO or _HALLUCINATION_RE.search(last):
            raw = " ".join(parts[:-1]).strip() if len(parts) > 1 else ""
            continue
        break
    return raw


_TRAILING_FILLERS = {"um", "umm", "uh", "uhh", "er", "erm", "hmm", "mm", "hm", "ah", "eh"}


def tail_status(text, end_words=None, cancel_words=None):
    """Pure. Judge a partial transcript at a pause: 'cancel' (whole thing is a cancel phrase), 'end' (ends with an
    end word like 'over'/'send it'), 'unfinished' (trails off on a filler/joining word, or nothing heard yet),
    otherwise 'finished'. Trailing silence-hallucinations are ignored, so they never count as an end word or speech."""
    text = strip_hallucinations(text)
    t = normalise(strip_wake_phrase(text or ""))
    # 'send it, um.' / 'over, uh': the filler-seeded tail prompt makes Whisper append fillers after the end word,
    # which judged it 'unfinished' and held the turn ~12 s (the user said 'send it' twice, 22:43). Ignore trailing fillers.
    t_end = t
    while t_end and t_end.split()[-1] in _TRAILING_FILLERS:
        t_end = " ".join(t_end.split()[:-1])
    for p in _phrases(end_words if end_words is not None else DEFAULT_END_WORDS):
        if t_end and (t_end == p or t_end.endswith(" " + p)):
            return "end"
    if not t:
        return "unfinished"
    if t in _phrases(cancel_words if cancel_words is not None else DEFAULT_CANCEL_WORDS):
        return "cancel"
    for p in _phrases(end_words if end_words is not None else DEFAULT_END_WORDS):
        if t == p or t.endswith(" " + p):
            return "end"
    if t.split()[-1] in CONTINUATION_WORDS:
        return "unfinished"
    if (text or "").rstrip().endswith((",", "...", "-", "…")):
        return "unfinished"
    return "finished"


def clean_command(text, end_words=None, cancel_words=None):
    """Pure. Final transcript -> (text, cancelled). Strips a trailing end word ('..., over' / 'send it');
    a transcript that is only a cancel phrase comes back as ('', True)."""
    raw = strip_hallucinations(text)
    words = raw.split()
    while words and normalise(words[-1]) in _TRAILING_FILLERS and len(words) > 1 \
            and any(normalise(" ".join(words[:-1])).endswith(p) for p in _phrases(end_words or DEFAULT_END_WORDS)):
        words = words[:-1]                       # 'send it, um.' -> 'send it' so the end word is still stripped
    raw = " ".join(words) if words else raw
    n = normalise(raw)
    if n and n in _phrases(cancel_words if cancel_words is not None else DEFAULT_CANCEL_WORDS):
        return "", True
    # Strip EVERY trailing end word, not just one: when the quick send doesn't fire he says it again ('..., send it,
    # send it' at 00:19 and 00:29 on 11 Oct, also 22:43 on 10 Oct), and stripping one left 'send it' in the text.
    phrases = sorted(_phrases(end_words if end_words is not None else DEFAULT_END_WORDS), key=len, reverse=True)
    stripped = False
    while True:
        for p in phrases:
            m = _end_phrase_re(p).search(raw)
            if m:
                raw, stripped = raw[:m.start()].rstrip(" ,.;:-!?\"'"), True
                break
        else:
            break
    if stripped and not normalise(raw):
        return "", False
    return raw, False


def _end_phrase_re(p):
    """'send it' -> regex matching it as whole trailing words in raw text, any punctuation between/after
    ('Send it.', 'send-it!', '"send it"'). Apostrophes optional inside words ("that's it" == 'thats it')."""
    words = [r"['’]?".join(re.escape(c) for c in w) for w in p.split()]
    return re.compile(r"(?:^|(?<=[^A-Za-z0-9'’]))" + r"[^A-Za-z0-9]+".join(words) + r"[^A-Za-z0-9]*$", re.I)


def ptt_clean(text, end_words=None, is_confirm=False):
    """Pure. Talk-key transcript -> text with a trailing end word ('..., send it') dropped. Left untouched when
    it doesn't end on an end word, when it is a guard confirmation ('send it' / 'yes, send it' must still reach
    CONFIRM_RE intact), or when the end word is all he said."""
    if is_confirm or tail_status(text or "", end_words, cancel_words=[]) != "end":
        return text
    cleaned, _ = clean_command(text, end_words, cancel_words=[])
    return cleaned or text


def end_word_sign_off(words, end_words=None, min_gap_s=0.5):
    """Pure. Talk-key pause check with word timings. words: [(text, start_s, end_s), ...] from Whisper
    (word_timestamps=True). True only when the trailing end word stands apart as a sign-off: it is all he said
    (fillers aside), or there is a gap of at least min_gap_s before it. 'take all of this over' spoken in one breath
    (16:18 on 11 Oct, cut the user off mid-sentence) has no gap, so it is not a sign-off. Without usable timings
    (words empty/None) returns None: the caller then relies on the post-word silence grace alone."""
    if not words:
        return None
    toks = []                                        # (normalised word, start, end), fillers/punctuation dropped
    for w, s, e in words:
        for piece in normalise(w).split():
            toks.append((piece, s, e))
    while toks and toks[-1][0] in _TRAILING_FILLERS:
        toks.pop()
    if not toks:
        return False
    for p in sorted(_phrases(end_words if end_words is not None else DEFAULT_END_WORDS),
                    key=lambda x: len(x.split()), reverse=True):
        pw = p.split()
        if [t[0] for t in toks[-len(pw):]] != pw:
            continue
        before = [t for t in toks[:-len(pw)] if t[0] not in _TRAILING_FILLERS]
        if not before:
            return True
        try:
            return float(toks[-len(pw)][1]) - float(before[-1][2]) >= float(min_gap_s)
        except (TypeError, ValueError):
            return None
    return False


def ptt_strip_if_fired(text, fired, end_words=None, is_confirm=False):
    """Pure. Talk-key final text: drop the trailing end word only when the end-word watcher actually fired
    (or it's an unambiguous multi-word one like 'send it'). A bare trailing 'over' he pressed the key after
    ('take this over') stays in his words."""
    if fired:
        return ptt_clean(text, end_words, is_confirm)
    multi = [p for p in _phrases(end_words if end_words is not None else DEFAULT_END_WORDS) if " " in p]
    return ptt_clean(text, multi, is_confirm) if multi else text


class WakeHit:
    __slots__ = ("score", "audio", "ended", "remainder")

    def __init__(self, score, audio, ended, remainder=None):
        self.score, self.audio, self.ended, self.remainder = float(score), audio, bool(ended), remainder


def _rms(np, x):
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


class WakeDetector:
    """Interface. feed() gets 16 kHz mono float32 chunks (any size) and returns a WakeHit when the wake word is heard."""
    engine = "none"

    def feed(self, chunk):
        raise NotImplementedError

    def reset(self):
        pass

    def close(self):
        pass


class WhisperWakeDetector(WakeDetector):
    """Fallback engine, needs no training. transcribe_fn(audio16k) -> text is injected (the app passes its loaded
    faster-whisper model), vad_fn(audio16k) -> bool is optional (Silero VAD via faster-whisper)."""
    engine = "whisper"

    def __init__(self, transcribe_fn, aliases=None, vad_fn=None, min_burst_s=0.4, max_check_s=1.5,
                 min_rms=0.006, gap_s=0.35, preroll_s=0.3, fuzzy=0.75, min_interval_s=0.0, clock=time.monotonic):
        import numpy as np
        self.np = np
        self.transcribe_fn, self.vad_fn = transcribe_fn, vad_fn
        self.aliases, self.fuzzy = aliases, fuzzy
        self.min_burst, self.max_check = int(min_burst_s * SR), int(max_check_s * SR)
        self.min_rms, self.gap = float(min_rms), int(gap_s * SR)
        self.preroll_n = int(preroll_s * SR)
        self.min_interval, self.clock, self.last_check = min_interval_s, clock, -1e9
        self.noise = 0.0
        self.checks = 0                       # how many transcriptions were spent (cost counter for tests/logs)
        self.reset()

    def reset(self):
        self.pre = self.np.zeros(0, dtype=self.np.float32)
        self.burst, self.burst_n, self.speech_n, self.silence_n = [], 0, 0, 0
        self.checked = False                  # already judged this burst: ignore the rest of it

    def _thr(self):
        return max(self.min_rms, self.noise * 3.0)

    def _judge(self, audio, ended):
        now = self.clock()
        if now - self.last_check < self.min_interval:
            return None
        self.last_check = now
        if self.vad_fn is not None:
            try:
                if not self.vad_fn(audio):
                    return None
            except Exception:
                pass
        self.checks += 1
        text = self.transcribe_fn(audio[: self.preroll_n + self.max_check]) or ""
        ok, rest = match_wake(text, self.aliases, self.fuzzy)
        if not ok:
            return None
        return WakeHit(1.0, audio, ended, rest)

    def feed(self, chunk):
        np = self.np
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if not len(chunk):
            return None
        loud = _rms(np, chunk) >= self._thr()
        if not self.burst_n:
            if not loud:
                self.noise = 0.95 * self.noise + 0.05 * _rms(np, chunk)
                self.pre = np.concatenate([self.pre, chunk])[-self.preroll_n:]
                return None
            self.burst, self.burst_n = [self.pre.copy()], len(self.pre)
            self.speech_n = self.silence_n = 0
            self.checked = False
        self.burst.append(chunk)
        self.burst_n += len(chunk)
        if loud:
            self.speech_n += self.silence_n + len(chunk)
            self.silence_n = 0
        else:
            self.silence_n += len(chunk)
        ended = self.silence_n >= self.gap
        hit = None
        if not self.checked and self.speech_n >= self.min_burst and (ended or self.speech_n >= self.max_check):
            self.checked = True
            hit = self._judge(np.concatenate(self.burst), ended)
        if ended:
            tail = np.concatenate(self.burst)[-self.preroll_n:]
            self.reset()
            self.pre = tail
        if hit is not None:
            self.reset()
        return hit


class OnnxScoreDetector(WakeDetector):
    """Primary engine for a trained 'hey aida' model (livekit-wakeword). Needs `pip install livekit-wakeword` and a model
    file; neither exists yet. Scores 80 ms frames; a hit needs `hits` frames in a row at or above threshold."""
    engine = "livekit"

    def __init__(self, model_path, threshold=0.6, hits=2):
        import numpy as np
        from livekit_wakeword import WakeWordModel          # ImportError -> caller falls back
        self.np, self.model = np, WakeWordModel(model_path)
        self.threshold, self.hits = float(threshold), int(hits)
        self.buf, self.run = np.zeros(0, dtype=np.float32), 0
        self.hist = np.zeros(0, dtype=np.float32)

    def reset(self):
        self.buf, self.run = self.np.zeros(0, dtype=self.np.float32), 0

    def feed(self, chunk):
        np = self.np
        self.buf = np.concatenate([self.buf, np.asarray(chunk, dtype=np.float32).reshape(-1)])
        self.hist = np.concatenate([self.hist, np.asarray(chunk, dtype=np.float32).reshape(-1)])[-2 * SR:]
        hit = None
        while len(self.buf) >= 1280:
            frame, self.buf = self.buf[:1280], self.buf[1280:]
            pred = self.model.predict((frame * 32767).astype(np.int16))
            score = max(pred.values()) if isinstance(pred, dict) else float(pred)
            self.run = self.run + 1 if score >= self.threshold else 0
            if self.run >= self.hits and hit is None:
                hit, self.run = WakeHit(score, self.hist.copy(), False, None), 0
        return hit


# ---------------------------------------------------------------- state machine
OFF, IDLE, WOKEN, CAPTURING, FOLLOWUP = "off", "idle", "woken", "capturing", "followup"


class WakeMachine:
    """Pure: feed(chunk, now, blocked, speaking) -> list of events (dicts). No threads, no I/O.
    Events: {"type": "wake", "score", "engine"}  {"type": "utterance", "audio", "via": "wake"|"followup"}
            {"type": "timeout"} (woken, nothing said)  {"type": "aborted"} (blocked mid-capture)"""

    def __init__(self, detector, followup_s=8.0, cooldown_s=1.5, listen_s=5.0, end_silence_s=0.8,
                 max_utterance_s=30.0, followup_min_s=0.5, min_rms=0.006, preroll_s=1.0, enabled=True,
                 end_silence_max_s=None, grace_s=0.0, tail_fn=None, end_words=None, cancel_words=None,
                 quick_end_s=0.8, tail_window_s=8.0, tail_min_interval_s=1.0, tail_rechecks=2, tail_recheck_s=2.5):
        """Patience (see config.yaml wake_*): a turn ends after end_silence_s + grace_s of quiet. With tail_fn
        (audio -> text, a short Whisper call) the last few seconds are transcribed once per pause (at most once a
        second): trailing 'um/and/so...' waits up to end_silence_max_s, an end word ('over') sends after
        quick_end_s, a bare 'cancel that' discards. Class defaults keep the old quick behaviour; make_machine()
        applies the patient config defaults."""
        import numpy as np
        self.np, self.det = np, detector
        self.followup_s, self.cooldown_s, self.listen_s = float(followup_s), float(cooldown_s), float(listen_s)
        self.end_silence_s, self.max_utt_n = float(end_silence_s), int(max_utterance_s * SR)
        self.end_silence_max_s = max(self.end_silence_s, float(end_silence_max_s if end_silence_max_s is not None
                                                               else end_silence_s))
        self.grace_s, self.tail_fn = max(0.0, float(grace_s)), tail_fn
        self.end_words, self.cancel_words = end_words, cancel_words
        self.quick_end_s, self.tail_window_n = float(quick_end_s), int(tail_window_s * SR)
        self.tail_min_interval_s, self.last_tail_at = float(tail_min_interval_s), -1e9
        self.tail_checks = 0                               # transcriptions spent on endpointing (cost counter)
        self.pause_verdict, self.pause_from = None, None    # verdict for the current pause (one check per pause)
        self.tail_rechecks, self.tail_recheck_s = int(tail_rechecks), float(tail_recheck_s)  # extra looks per pause
        self.tail_rechecks_left, self.quiet_peak = self.tail_rechecks, 0.0
        self.tail_trace = []                               # (verdict, text) of recent checks, drained by the listener
        self.patient = self.grace_s > 0 or self.tail_fn is not None or self.end_silence_s >= 1.0
        self.followup_min_n, self.min_rms = int(followup_min_s * SR), float(min_rms)
        self.preroll_n = int(preroll_s * SR)
        self.chime_s = 0.35
        self.state = IDLE if enabled else OFF
        self.ring = np.zeros(0, dtype=np.float32)          # pre-roll, RAM only, ~1 s
        self.cap, self.cap_n = [], 0
        self.last_speech = 0.0
        self.cooldown_until = 0.0
        self.followup_until = 0.0
        self.awaiting_reply, self.seen_blocked, self.saw_speaking, self.sent_at = False, False, False, 0.0
        self.fu_speech_n = 0
        self.fu_start_n = 0

    # -- switches
    def set_enabled(self, on):
        if on and self.state == OFF:
            self.state = IDLE
        elif not on:
            self._drop()
            self.state = OFF

    def _drop(self):
        self.cap, self.cap_n, self.fu_speech_n = [], 0, 0
        self.tail_rechecks_left, self.quiet_peak = self.tail_rechecks, 0.0
        self.ring = self.np.zeros(0, dtype=self.np.float32)
        try:
            self.det.reset()
        except Exception:
            pass

    def _loud(self, chunk):
        return _rms(self.np, chunk) >= self.min_rms

    def _send(self, now, via, reason=None, quiet_s=None):
        audio = self.np.concatenate(self.cap) if self.cap else self.np.zeros(0, dtype=self.np.float32)
        peak = self.quiet_peak                       # read before _drop() resets it
        self._drop()
        self.state = IDLE
        self.cooldown_until = now + self.cooldown_s
        self.awaiting_reply, self.seen_blocked, self.saw_speaking, self.sent_at = True, False, False, now
        ev = {"type": "utterance", "audio": audio, "via": via}
        if reason is not None:                       # why the turn closed (logged; no text): end / finished /
            ev["reason"] = reason                    # unfinished / silence (no tail check) / max
            ev["quiet_s"] = round(float(quiet_s or 0.0), 2)
            ev["peak_rms"] = round(float(peak), 4)   # loudest chunk in that 'quiet' (vs min_rms)
            ev["tail_checks"] = self.tail_checks
        return ev

    def feed(self, chunk, now, blocked=False, speaking=False):
        np = self.np
        if self.state == OFF:
            return []
        ev = []
        if blocked:                                   # AIDA talking / PTT / job running: hear nothing at all
            if self.state in (WOKEN, CAPTURING):
                ev.append({"type": "aborted"})
            if self.state != IDLE or self.cap or len(self.ring):
                self._drop()
            if self.state != FOLLOWUP:
                self.state = IDLE
            if self.awaiting_reply:
                self.seen_blocked = True
                self.saw_speaking = self.saw_speaking or bool(speaking)
            return ev
        # unblocked: open the follow-up window once a spoken reply has finished
        if self.awaiting_reply:
            if self.seen_blocked:
                self.awaiting_reply = False
                if self.saw_speaking and self.followup_s > 0:
                    self.state, self.followup_until, self.fu_speech_n = FOLLOWUP, now + self.followup_s, 0
            elif now - self.sent_at > 15:
                self.awaiting_reply = False
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        loud = self._loud(chunk)

        if self.state == FOLLOWUP:
            if now > self.followup_until and not self.fu_speech_n:
                self.state = IDLE
            else:
                if loud:
                    if not self.fu_speech_n:
                        self.cap, self.cap_n = [self.ring[-int(0.3 * SR):].copy()], 0
                    self.cap.append(chunk)
                    self.cap_n += len(chunk)
                    self.fu_speech_n += len(chunk)
                    self.last_speech = now
                    if self.fu_speech_n >= self.followup_min_n:
                        self.state = CAPTURING
                        self.cap_via = "followup"
                        return ev
                elif self.fu_speech_n:
                    self.cap.append(chunk)
                    if now - self.last_speech > 0.4:     # too short to be a turn: breath, cough, click
                        self.cap, self.cap_n, self.fu_speech_n = [], 0, 0
                self.ring = np.concatenate([self.ring, chunk])[-self.preroll_n:]
                return ev

        if self.state == IDLE:
            self.ring = np.concatenate([self.ring, chunk])[-self.preroll_n:]
            if now < self.cooldown_until:
                return ev
            try:
                hit = self.det.feed(chunk)
            except Exception as e:
                return ev + [{"type": "error", "error": f"{e.__class__.__name__}: {e}"}]
            if hit is None:
                return ev
            ev.append({"type": "wake", "score": hit.score, "engine": getattr(self.det, "engine", "?")})
            audio = hit.audio if hit.audio is not None and len(hit.audio) else self.ring.copy()
            self.cap, self.cap_n = [np.asarray(audio, dtype=np.float32)], len(audio)
            self.last_speech = now
            self.cap_via = "wake"
            if hit.ended and hit.remainder and self.patient:   # "hey aida open the..." + short pause: keep listening
                self.state = CAPTURING
                self.pause_verdict, self.pause_from = None, None
            elif hit.ended and hit.remainder:         # legacy quick mode: already complete
                ev.append(self._send(now, "wake"))
            elif hit.ended or hit.remainder == "":
                self.state = WOKEN                    # "hey aida" ... wait for the command
                self.woken_at = now
            else:
                self.state = CAPTURING                # still talking in one breath
            return ev

        if self.state == WOKEN:
            self.cap.append(chunk)
            self.cap_n += len(chunk)
            if loud and now - self.woken_at >= self.chime_s:   # the wake chime itself must not count as speech
                self.state, self.last_speech = CAPTURING, now
            elif now - self.woken_at >= self.listen_s:
                self._drop()
                self.state = IDLE
                self.cooldown_until = now + self.cooldown_s
                ev.append({"type": "timeout"})
            return ev

        if self.state == CAPTURING:
            self.cap.append(chunk)
            self.cap_n += len(chunk)
            if loud:
                self.last_speech = now
                self.pause_verdict, self.pause_from = None, None    # talking again: a new pause gets a new look
                self.tail_rechecks_left, self.quiet_peak = self.tail_rechecks, 0.0
            if self.cap_n >= self.max_utt_n:
                ev.append(self._send(now, getattr(self, "cap_via", "wake"), reason="max"))
                return ev
            if not loud:
                self.quiet_peak = max(self.quiet_peak, _rms(np, chunk))   # diagnostics: was he murmuring?
            quiet = now - self.last_speech
            if self.tail_fn is not None and self.pause_verdict is None and quiet >= min(self.quick_end_s,
                                                                                         self.end_silence_s):
                if now - self.last_tail_at >= self.tail_min_interval_s:
                    self.pause_verdict = self._tail_check(now)
                    self.pause_from = self.last_speech
            elif (self.tail_fn is not None and self.pause_verdict in ("finished", "unfinished")
                  and self.tail_rechecks_left > 0 and now - self.last_tail_at >= self.tail_recheck_s):
                # 11 Oct 00:19/00:29: a trailing 'send it' didn't send, so he said it again. The single check at
                # 0.8 s can miss it (said softly under min_rms so the pause began before it, or a bad short
                # transcription). Look again a couple of times during the long wait; only 'end'/'cancel' change things.
                self.tail_rechecks_left -= 1
                v2 = self._tail_check(now)
                if v2 in ("end", "cancel"):
                    self.pause_verdict = v2
            v = self.pause_verdict
            if v == "cancel":
                self._drop()
                self.state = IDLE
                self.cooldown_until = now + self.cooldown_s
                ev.append({"type": "discarded"})
                return ev
            if v == "end":
                need = self.quick_end_s
            elif v == "unfinished":
                need = self.end_silence_max_s + self.grace_s
            else:
                need = self.end_silence_s + self.grace_s
            if quiet >= need:
                ev.append(self._send(now, getattr(self, "cap_via", "wake"), reason=v or "silence", quiet_s=quiet))
            return ev
        return ev

    def _tail_check(self, now):
        """One short transcription of the last few seconds; 'finished' if it fails (base wait still applies)."""
        self.last_tail_at = now
        self.tail_checks += 1
        try:
            audio = self.np.concatenate(self.cap)[-self.tail_window_n:]
            text = self.tail_fn(audio) or ""
            v = tail_status(text, self.end_words, self.cancel_words)
        except Exception as e:
            text, v = f"<tail check failed: {e.__class__.__name__}>", "finished"
        self.tail_trace = (self.tail_trace + [(v, text)])[-6:]   # so the day log shows what the judge heard
        return v


# ---------------------------------------------------------------- logging (no audio, no text)
class WakeLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, event, **fields):
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
        for k, v in fields.items():
            if k in ("score", "engine", "via", "seconds", "error", "cpu_pct", "reason", "quiet_s", "peak_rms",
                     "tail_checks"):
                rec[k] = round(v, 3) if isinstance(v, float) else v
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
        except OSError:
            pass


def to_16k(np, audio, rate):
    if rate == SR or not len(audio):
        return audio.astype(np.float32)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(SR, int(rate))
        return resample_poly(audio, SR // g, int(rate) // g).astype(np.float32)
    except Exception:
        n = int(len(audio) * SR / rate)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------- the listener thread
class WakeListener:
    """Owns one sounddevice InputStream. ctx() -> (blocked, speaking) is asked for every chunk.
    on_utterance(audio16k, via) runs on a fresh thread so the mic never stalls. Never raises out of start()."""

    def __init__(self, machine, ctx, on_utterance, on_wake=None, device=None, rate=None, sd=None,
                 log=None, wake_log=None, block_s=0.08):
        import numpy as np
        self.np, self.m, self.ctx = np, machine, ctx
        self.on_utterance, self.on_wake = on_utterance, on_wake
        self.device, self.rate, self.sd = device, rate, sd
        self.log = log or (lambda who, text: None)
        self.wake_log = wake_log
        self.block_s = block_s
        self.q = queue.Queue(maxsize=200)
        self.stop_evt = threading.Event()
        self.stream, self.thread = None, None
        self.error = ""
        self.dropped = 0

    @property
    def state(self):
        if self.error:
            return "unavailable"
        return self.m.state if self.thread is not None else OFF

    def _wl(self, event, **kw):
        if self.wake_log is not None:
            kw.setdefault("engine", getattr(self.m.det, "engine", "?"))
            self.wake_log.write(event, **kw)

    def start(self):
        try:
            sd = self.sd
            if sd is None:
                import sounddevice as sd
                self.sd = sd
            if not self.rate:
                info = sd.query_devices(self.device, "input") if self.device is not None else sd.query_devices(kind="input")
                self.rate = int(info["default_samplerate"])

            def cb(data, n, t, s):
                try:
                    self.q.put_nowait(data[:, 0].copy() if getattr(data, "ndim", 1) > 1 else data.copy())
                except queue.Full:
                    self.dropped += 1

            self.stream = sd.InputStream(device=self.device, channels=1, samplerate=self.rate, dtype="float32",
                                         blocksize=int(self.rate * self.block_s), callback=cb)
            self.stream.start()
        except Exception as e:
            self.error = f"{e.__class__.__name__}: {e}"
            self.stream = None
            self.log("error", f"Open mic unavailable ({self.error}); push-to-talk still works.")
            self._wl("error", error=self.error)
            return False
        self.error = ""
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._run, name="wake-listener", daemon=True)
        self.thread.start()
        self._wl("start")
        return True

    def _run(self):
        while not self.stop_evt.is_set():
            try:
                data = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            self.process(data, time.monotonic())

    def process(self, data, now):
        """One block from the mic (device rate). Public so tests can drive it without a thread."""
        try:
            blocked, speaking = self.ctx()
        except Exception:
            blocked, speaking = True, False
        chunk = to_16k(self.np, self.np.asarray(data, dtype=self.np.float32).reshape(-1), self.rate or SR)
        try:
            events = self.m.feed(chunk, now, blocked=blocked, speaking=speaking)
        except Exception as e:
            events = [{"type": "error", "error": f"{e.__class__.__name__}: {e}"}]
        for e in events:
            kind = e["type"]
            if kind == "wake":
                self._wl("wake", score=e["score"], engine=e["engine"])
                if self.on_wake:
                    try:
                        self.on_wake()
                    except Exception:
                        pass
            elif kind == "utterance":
                extra = {k: e[k] for k in ("reason", "quiet_s", "peak_rms", "tail_checks") if k in e}
                self._wl("utterance", via=e["via"], seconds=len(e["audio"]) / SR, **extra)
                trace = list(getattr(self.m, "tail_trace", []) or [])
                self.m.tail_trace = []
                if extra or trace:                   # day log (already holds his words): why the turn closed
                    self.log("wake", "Turn closed: " + ", ".join(f"{k}={v}" for k, v in extra.items())
                             + "".join(f" | judge {v}: '{t[-120:]}'" for v, t in trace[-3:]))
                threading.Thread(target=self._deliver, args=(e["audio"], e["via"]), daemon=True).start()
            elif kind == "error":
                self._wl("error", error=e["error"])
            else:
                self._wl(kind)
        return events

    def _deliver(self, audio, via):
        try:
            self.on_utterance(audio, via)
        except Exception as e:
            self.log("error", f"Open mic hand-off failed: {e}")

    def stop(self):
        self.stop_evt.set()
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        t, self.thread = self.thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        while not self.q.empty():                    # nothing of the mic stays in memory after stop
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        try:
            self.m._drop()
            self.m.det.close()
        except Exception:
            pass
        self._wl("stop")


# ---------------------------------------------------------------- factory
def silero_vad_fn():
    """Silero VAD from faster-whisper (already installed). Returns vad(audio16k) -> bool or None if unavailable."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return None
    opts = VadOptions(threshold=0.4, min_speech_duration_ms=200, min_silence_duration_ms=100)

    def vad(audio):
        return bool(get_speech_timestamps(audio, opts))
    return vad


def make_detector(cfg, transcribe_fn, log=None):
    """Pick the engine from config (wake_engine: auto | livekit | whisper). Anything missing falls back to whisper."""
    log = log or (lambda who, text: None)
    want = str(cfg.get("wake_engine", "auto") or "auto").lower()
    aliases = cfg.get("wake_aliases") or DEFAULT_ALIASES
    if want in ("auto", "livekit"):
        model = str(cfg.get("wake_model", "") or "")
        if model:
            try:
                return OnnxScoreDetector(model, threshold=float(cfg.get("wake_threshold", 0.6)))
            except Exception as e:
                log("setup", f"Wake engine livekit unavailable ({e.__class__.__name__}); using the Whisper fallback.")
        elif want == "livekit":
            log("setup", "Wake engine livekit needs wake_model (a trained hey-aida .onnx); using the Whisper fallback.")
    return WhisperWakeDetector(transcribe_fn, aliases=aliases, vad_fn=silero_vad_fn(),
                               fuzzy=float(cfg.get("wake_fuzzy", 0.75)),
                               min_rms=float(cfg.get("wake_min_rms", 0.006)))


def make_machine(cfg, detector, tail_fn=None):
    """tail_fn(audio16k) -> text: optional short transcription used only at pauses (patience check)."""
    return WakeMachine(detector, followup_s=float(cfg.get("wake_followup_s", 8)),
                       cooldown_s=float(cfg.get("wake_cooldown_s", 1.5)),
                       min_rms=float(cfg.get("wake_min_rms", 0.006)),
                       end_silence_s=float(cfg.get("wake_end_silence_s", 8)),
                       end_silence_max_s=float(cfg.get("wake_end_silence_max_s", 10)),
                       max_utterance_s=float(cfg.get("wake_max_utterance_s", 120)),
                       grace_s=float(cfg.get("wake_grace_s", 2)),
                       end_words=cfg.get("wake_end_words") or DEFAULT_END_WORDS,
                       cancel_words=cfg.get("wake_cancel_words") or DEFAULT_CANCEL_WORDS,
                       tail_fn=tail_fn)
