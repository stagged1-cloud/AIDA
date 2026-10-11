"""Screen recording control: pure logic, no Tk, no network. The real recorder is obs_control.ObsRecorder (OBS Studio via
obs-websocket); everything here talks to it through its small dict-returning interface, so tests use a fake.

  * parse_record_command(text)   spoken/typed words -> ('start'|'stop'|'status'|None, monitor or None)
  * parse_monitor_answer(text)   the answer to "which screen?" -> 'left'|'middle'|'right'|int ordinal|None
  * label_monitors(monitors)     OBS monitor list -> labelled list (left/middle/right by position, resolution)
  * ScreenRecordController       start/stop/toggle on a background thread, state, plain-English errors

A 'monitor' in a command is 'left' | 'middle' | 'right' or a 1-based int ordinal counted left to right.
The controller never guesses: with more than one screen and none chosen it calls `controller.choose(labelled, source)`.
"""
import os
import re
import threading
import time

# ---------------------------------------------------------------- parsing
_SIDES = {"left": "left", "middle": "middle", "centre": "middle", "center": "middle", "laptop": "middle",
          "main": "middle", "right": "right"}
_NUMS = {"one": 1, "1": 1, "first": 1, "two": 2, "2": 2, "second": 2, "to": 2, "too": 2,
         "three": 3, "3": 3, "third": 3}
_SIDE_RE = r"left|middle|centre|center|laptop|main|right"
_NUM_RE = r"one|two|three|1|2|3|first|second|third|to|too"
# "the left screen", "my right hand monitor", "screen two", "the screen on the left"
_SPEC = re.compile(
    rf"(?:(?:the|my) )?(?:(?P<side>{_SIDE_RE})(?: hand)?(?: side)? )?(?:screen|monitor|display)"
    rf"(?: (?P<num>{_NUM_RE}))?(?: on the (?P<side2>left|middle|right)(?: hand)?(?: side)?)?")
_LEAD = re.compile(r"^(?:(?:ok|okay|hey|hi|right|so|please|aida|can you|could you|would you|will you|i want you to|"
                   r"i need you to|go ahead and|just|now|lets|let us)\s+)+")
_TAIL = re.compile(r"(?:\s+(?:please|now|thanks|thank you|for me|will you|will ya|mate|buddy|aida))+$")
_START = [re.compile(r"(?:start|begin) (?:a |the |my )?(?:screen )?(?:recording|record)(?: of)?(?: (?P<rest>.+))?"),
          re.compile(r"screen record(?:ing)?(?: (?P<rest>.+))?"),
          re.compile(r"record (?P<rest>.+)")]                      # bare 'record X' needs X to be a screen phrase
_STOP = re.compile(r"(?:stop|end|finish|halt) (?:the |my |this )?(?:screen )?(?:recording|record)(?: (?:of )?(?:the |my )?screen)?")
_STATUS = [re.compile(r"(?:are you|are we|is it|is the screen|is this|am i) (?:still )?(?:screen )?recording(?: the screen)?"),
           re.compile(r"is the (?:screen )?recording (?:still )?(?:on|running|going)"),
           re.compile(r"recording status")]


def _norm(text, lead=True):
    t = re.sub(r"[^a-z0-9' ]", " ", (text or "").lower().replace("’", "'"))
    t = re.sub(r"'", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if lead:                       # 'right,' as a filler opener would otherwise eat the answer 'right one'
        t = _LEAD.sub("", t)
    return _TAIL.sub("", t).strip()


def _spec_to_monitor(rest, allow_bare_side=False):
    """'the left screen' -> 'left'; 'screen two' -> 2; 'the screen' -> None; not a screen phrase -> False."""
    if not rest:
        return None
    m = _SPEC.fullmatch(rest.strip())
    if not m:
        if allow_bare_side:                      # 'start screen recording on the left'
            b = re.fullmatch(rf"(?:on )?(?:the )?(?P<s>{_SIDE_RE})(?: hand)?(?: side)?", rest.strip())
            if b:
                return _SIDES[b.group("s")]
        return False
    if m.group("side"):
        return _SIDES[m.group("side")]
    if m.group("side2"):
        return m.group("side2")
    if m.group("num"):
        return _NUMS[m.group("num")]
    return None


_A_WORD = r"(?:(?:the|my|pc|desktop|computer) )*(?:sound|audio|mic|microphone)"
_AUDIO_CLAUSE = re.compile(rf"(?: |^)(?P<pol>with|without|no)(?: (?P<a>{_A_WORD})(?: (?:and|plus) (?P<b>{_A_WORD}))?)$")


def _audio_flags(pol, *words):
    """with/without + audio words -> {'desktop': bool|None, 'mic': bool|None}. An explicit 'with' sets the one(s) named
    on and the other off; 'without sound' = silent (both off); 'without the mic' only turns the mic off."""
    named = {("mic" if w.endswith(("mic", "microphone")) else "desktop") for w in words if w}
    if pol == "with":
        generic = any(w and re.fullmatch(r"(?:(?:the|my) )*(?:sound|audio)", w) for w in words)
        if generic and named == {"desktop"}:     # bare 'with sound' = everything audible, his voice too
            return {"desktop": True, "mic": True}
        return {"desktop": "desktop" in named, "mic": "mic" in named}
    if "desktop" in named:
        return {"desktop": False, "mic": False}
    return {"desktop": None, "mic": False}


def parse_record_command_ex(text):
    """(verb, monitor, audio). audio is None when no sound words were spoken, else {'desktop','mic'} with True/False/None
    (None = use the remembered choice). 'start recording with sound' -> desktop on, mic off; 'record with the microphone'
    -> mic only; 'record with sound and mic' -> both; 'record without sound' -> both off."""
    r = _parse_whole(text)
    if r[0] is not None:
        return r
    return _parse_embedded(text)


def _parse_embedded(text):
    """Open-mic speech is a run-on: 'okay, so, um, I want you to record the screen for me, screen one, thanks, um, what
    else'. Try each comma/sentence clause on its own (start/stop only, never status), and let a following clause that is
    just a screen answer ('screen one') pick the monitor."""
    # Strict (10 Oct 22:43): only when EVERY other clause is filler. Any extra instruction ('for 30 seconds',
    # 'then open it and play it', chatter) means the whole utterance goes to Claude; ambiguous = Claude.
    parts = [p for p in re.split(r"[,.;!?]+", text or "") if p.strip()]
    if len(parts) < 2:
        return None, None, None
    parts = [p for p in parts if not _is_filler_clause(p)]
    if not parts or len(parts) > 2:
        return None, None, None
    verb, mon, audio = _parse_whole(parts[0])
    if verb not in ("start", "stop"):
        return None, None, None
    if len(parts) == 2:
        nxt = parse_monitor_answer(parts[1]) if verb == "start" and mon is None else None
        if nxt is None:
            return None, None, None
        mon = nxt
    return verb, mon, audio


_FILLER_WORDS = {"um", "umm", "uh", "uhh", "er", "erm", "hmm", "mm", "ah", "eh", "and", "so", "ok", "okay", "right",
                 "well", "yeah", "yes", "please", "thanks", "cheers", "now", "mate", "buddy", "anyway"}
_FILLER_PHRASES = ["hey aida", "send it", "what else", "thats it", "thats enough", "thank you", "over"]


def _is_filler_clause(part):
    """'um', 'and um', 'ok', 'send it', 'thanks', 'what else' -> True. Anything with a real word -> False."""
    t = " " + re.sub(r"[^a-z0-9 ]", " ", (part or "").lower().replace("'", "").replace("’", "")) + " "
    for ph in _FILLER_PHRASES:
        t = re.sub(rf"\s{ph}(?=\s)", " ", t)
    return all(w in _FILLER_WORDS for w in t.split())


def _parse_whole(text):
    verb, mon = _parse_plain(text)
    if verb is not None:
        return verb, mon, None
    t = _norm(text)
    t = t.replace(" with no ", " without ")
    m = _AUDIO_CLAUSE.search(t) if t else None
    if not m:
        return None, None, None
    rest = t[:m.start()].strip()
    if rest == "record":
        rest = "start recording"
    verb, mon = _parse_plain(rest)
    if verb != "start":
        return None, None, None
    return "start", mon, _audio_flags(m.group("pol"), m.group("a"), m.group("b"))


def parse_record_command(text):
    """('start'|'stop'|'status'|None, monitor|None). Only whole-sentence screen-recording phrases count, so 'record the
    meeting notes in a file' or "what's the world record" return (None, None). Sound words are accepted
    (see parse_record_command_ex) and dropped here."""
    verb, mon, _ = parse_record_command_ex(text)
    return verb, mon


def _parse_plain(text):
    t = _norm(text)
    if not t:
        return None, None
    if _STOP.fullmatch(t):
        return "stop", None
    if any(p.fullmatch(t) for p in _STATUS):
        return "status", None
    for i, p in enumerate(_START):
        m = p.fullmatch(t)
        if not m:
            continue
        rest = m.group("rest")
        if i == 2 and not rest:
            continue
        mon = _spec_to_monitor(rest, allow_bare_side=i < 2)
        if mon is False:
            continue
        return "start", mon
    return None, None


_ANSWER = re.compile(rf"(?:(?:the|my|number|screen|monitor|display) )*(?:(?P<side>{_SIDE_RE})|(?P<num>{_NUM_RE}))"
                     r"(?: hand)?(?: side)?(?: (?:one|screen|monitor|display))?")


def parse_monitor_answer(text):
    """Answer to 'which screen?': 'left' / 'the middle one' / 'screen two' / 'three' -> 'left'|'middle'|'right'|int."""
    t = _norm(text, lead=False)
    m = _ANSWER.fullmatch(t) if t else None
    if not m:
        return None
    return _SIDES[m.group("side")] if m.group("side") else _NUMS[m.group("num")]


# ---------------------------------------------------------------- monitors
def label_monitors(monitors):
    """Sort left to right (by rect when every screen has one, else by index) and label them.
    Returns [{'index','name','ordinal','label','size','desc'}]. Labels: 2 screens = left/right, 3 = left/middle/right."""
    mons = [m for m in (monitors or []) if isinstance(m, dict) and "index" in m]
    rects = all(isinstance(m.get("rect"), (list, tuple)) and len(m["rect"]) == 4 for m in mons)
    mons = sorted(mons, key=(lambda m: (m["rect"][0], m["index"])) if rects else (lambda m: m["index"]))
    n = len(mons)
    names = {2: ["left", "right"], 3: ["left", "middle", "right"]}.get(n)
    out = []
    for i, m in enumerate(mons):
        size = ""
        if rects:
            l, t, r, b = m["rect"]
            size = f"{int(r - l)}x{int(b - t)}"
        label = names[i] if names else None
        base = f"{label.capitalize()} screen" if label else f"Screen {i + 1}"
        extra = ", ".join(x for x in (size, m.get("name") or "") if x)
        text = f"Screen {i + 1}" + (f" ({label})" if label else "") + (f" {size}" if size else "")
        # rect MUST be passed on: the picker numbers screens by rect.left; without it it fell back to OBS index order,
        # which is not left-to-right (OBS listed the right-hand monitor first), so 'Screen 1' recorded Screen 2.
        out.append({"index": m["index"], "name": m.get("name") or "", "ordinal": i + 1, "number": i + 1, "label": label,
                    "rect": list(m["rect"]) if rects else None,
                    "size": size, "text": text, "desc": f"{base} ({extra})" if extra else base})
    return out


def resolve_monitor(spec, labelled):
    """spec ('left'|'middle'|'right'|int ordinal) -> OBS monitor index, or None if it doesn't fit these screens."""
    if spec is None:
        return None
    for m in labelled:
        if (isinstance(spec, int) and m["ordinal"] == spec) or (isinstance(spec, str) and m["label"] == spec):
            return m["index"]
    return None


def name_of(labelled, index):
    for m in labelled:
        if m["index"] == index:
            return f"the {m['label']} screen" if m["label"] else (f"screen {m['ordinal']}" if len(labelled) > 1
                                                                  else "the screen")
    return "the screen"


def choice_question(labelled):
    """Spoken 'which screen?' with the options the user can answer with."""
    opts = [m["label"] or str(m["ordinal"]) for m in labelled]
    if not opts:
        return "Which screen?"
    return "Which screen? " + (", ".join(opts[:-1]) + " or " + opts[-1] if len(opts) > 1 else opts[0]) + "?"


# ---------------------------------------------------------------- errors
def map_error(err, action="record"):
    """Raw recorder error -> one plain sentence the user can act on."""
    e = (err or "").strip()
    low = e.lower()
    if re.search(r"controlled folder|^obs was blocked|^obs.s video encoder|^obs stopped the recording|"
                 r"did not start recording within", low):
        return e if e.endswith((".", "!", "?")) else e + "."          # already a plain, specific sentence
    if re.search(r"not recording", low):
        return "Not recording."
    if re.search(r"password|auth", low):
        return "OBS refused the password. Check the OBS password in config.yaml matches OBS's WebSocket settings."
    if re.search(r"not installed|can.?t find obs|couldn.?t find obs|no obs|obs64|find_obs|executable", low):
        return "I can't find OBS Studio. Install it, or set its path in config.yaml."
    if re.search(r"launch|not running|isn.?t running|start obs", low):
        return "OBS isn't running and I couldn't start it. Open OBS yourself and try again."
    if re.search(r"refused|connect|timed? ?out|unreachable|websocket|socket|10061", low):
        return ("I can't reach OBS. In OBS, open Tools, WebSocket Server Settings and tick Enable WebSocket Server.")
    if re.search(r"monitor|display|screen|source|scene", low):
        return "OBS has no usable screen to record. Check the display capture in OBS."
    if re.search(r"disk|space|permission|denied|path|folder|output", low):
        return f"OBS couldn't {action} to disk. Check its recording folder has space and is writable."
    if not e:
        return f"OBS couldn't {action}, and gave no reason."
    first = re.split(r"(?<=[.!?])\s", e)[0].rstrip(".")
    return f"OBS couldn't {action}: {first[:140]}."


def audio_phrase(audio):
    """[] -> ' with no sound'; ['desktop'] -> ' with PC sound'; None (recorder did not say) -> ''."""
    if audio is None:
        return ""
    names = [{"desktop": "PC sound", "mic": "the microphone"}.get(a, a) for a in audio]
    return " with " + (" and ".join(names) if names else "no sound")


def _short_path(p):
    p = str(p or "").replace("\\", "/")
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[-2:]) if len(parts) > 1 else p


# ---------------------------------------------------------------- controller
class ScreenRecordController:
    """States: idle -> starting -> recording -> stopping -> idle. Every recorder call runs on a worker thread.

    speak(text)        voice reply (only used for source='voice')
    notify(kind, text) log line, kind is 'info' or 'error' (always used)
    choose(labelled, source)  set by the owner; called (on the worker thread) when a screen choice is needed
    """

    def __init__(self, recorder, speak=None, notify=None, autolaunch=True, launch_timeout=30.0, poll_interval=1.0):
        self.last_paths = []
        self.screens = []
        self.audio = None                 # active audio sources of the current recording ('desktop', 'mic'); None = unknown
        self._opts = {}
        self.rec,self._speak, self._notify = recorder, speak, notify
        self.autolaunch, self.launch_timeout, self.poll_interval = autolaunch, launch_timeout, poll_interval
        self.choose = None
        self.state = "idle"
        self.started_at = None
        self.last_path = None
        self.last_error = ""
        self.screen_name = ""
        self._lock = threading.Lock()
        self._thread = None
        self._gen = 0                     # bumped by start/stop so a status poll begun earlier is discarded
        self._watch = None

    # -- follow OBS's real state (it can be stopped/started outside the app: OBS itself, another process)
    def sync(self):
        """Ask OBS whether it is recording and correct self.state to match. Never raises, never touches a start or
        stop in progress, and discards the answer if a start/stop began while it was being fetched. Returns True if
        the state changed."""
        fn = getattr(self.rec, "status", None)
        if fn is None:
            return False
        with self._lock:
            if self.state not in ("idle", "recording"):
                return False
            gen = self._gen
        try:
            s = fn() or {}
        except Exception:
            return False
        if not s.get("ok"):
            if self.state == "recording" and s.get("obs_running") is False and not s.get("connected"):
                with self._lock:                         # OBS has gone away entirely: nothing is recording
                    if self._gen == gen and self.state == "recording":
                        self.started_at, self.state = None, "idle"
                        return True
            return False
        live = bool(s.get("recording"))
        with self._lock:
            if self._gen != gen or self.state not in ("idle", "recording"):
                return False
            if self.state == "recording" and not live:
                self.started_at, self.state = None, "idle"
                return True
            if self.state == "idle" and live:
                try:
                    secs = max(0.0, float(s.get("seconds") or 0))
                except (TypeError, ValueError):
                    secs = 0.0
                self.started_at, self.state, self.screen_name, self.audio = time.time() - secs, "recording", "", None
                return True
        return False

    def watch(self, interval=2.0):
        """Background daemon that calls sync() every interval seconds (off the Tk thread). Idempotent."""
        if self._watch and self._watch.is_alive():
            return
        def loop():
            while True:
                time.sleep(interval)
                try:
                    self.sync()
                except Exception:
                    pass
        self._watch = threading.Thread(target=loop, daemon=True, name="obs-status-watch")
        self._watch.start()

    # -- helpers
    def _say(self, kind, text, source, spoken=None):
        if kind == "error":
            self.last_error = text
        for fn, args in ((self._notify, (kind, text)),
                         (self._speak if source == "voice" else None, (spoken or text,))):
            if fn:
                try:
                    fn(*args)
                except Exception:
                    pass

    def _run(self, fn, *a):
        t = threading.Thread(target=fn, args=a, daemon=True)
        self._thread = t
        t.start()

    def join(self, timeout=5.0):
        """Wait for the current worker (tests; the app never needs this)."""
        t = self._thread
        if t:
            t.join(timeout)

    def elapsed(self):
        return 0 if self.state != "recording" or not self.started_at else max(0, int(time.time() - self.started_at))

    def is_busy(self):
        return self.state in ("starting", "stopping")

    def status_text(self):
        if self.state == "recording":
            s = self.elapsed()
            return f"Yes, recording {self.screen_name or 'the screen'}, {s // 60} minutes {s % 60} seconds so far."
        if self.state in ("starting", "stopping"):
            return f"Just {self.state} the recording."
        return "No, I'm not recording."

    def dialog_options(self):
        """Remembered choices + microphone list + folder check for the record dialog. Never raises; a recorder that
        cannot supply them (or OBS being down) gives defaults with only 'Default' as the microphone."""
        out = {"prefs": {}, "devices": [{"id": "default", "name": "Default"}], "warn": None}
        try:
            import obs_control
            out["warn"] = obs_control.folder_warning
        except Exception:
            pass
        try:
            fn = getattr(self.rec, "prefs", None)
            if fn:
                out["prefs"] = dict(fn())
            fn = getattr(self.rec, "list_mic_devices", None)
            if fn:
                r = fn() or {}
                if r.get("devices"):
                    out["devices"] = list(r["devices"])
        except Exception:
            pass
        return out

    # -- public actions; return at once with one of: starting, stopping, busy, already, not_recording
    def start(self, monitor=None, source="ui", index=None, monitors=None, audio_desktop=None, audio_mic=None,
              record_dir=None, mic_device=None, remember=True):
        """monitor: 'left'/'middle'/'right'/ordinal (voice). index: one OBS monitor index. monitors: list of OBS
        monitor indexes (the picker); two or more are recorded together into one wide file. audio_* / record_dir /
        mic_device: None = the recorder's remembered choice (only passed on when set)."""
        opts = {k: v for k, v in (("audio_desktop", audio_desktop), ("audio_mic", audio_mic),
                                  ("record_dir", record_dir), ("mic_device", mic_device)) if v is not None}
        if not remember:
            opts["remember"] = False
        with self._lock:
            if self.state == "recording":
                return "already"
            if self.state != "idle":
                return "busy"
            self.state = "starting"
            self._gen += 1
            self._opts = opts
        idxs = None
        if monitors is not None:
            idxs = [int(i) for i in monitors if i is not None]
        elif index is not None:
            idxs = [int(index)]
        self._run(self._do_start, monitor, source, idxs)
        return "starting"

    def stop(self, source="ui"):
        with self._lock:
            if self.state == "idle":
                return "not_recording"
            if self.state != "recording":
                return "busy"
            self.state = "stopping"
            self._gen += 1
        self._run(self._do_stop, source)
        return "stopping"

    def toggle(self, source="ui", monitor=None):
        return self.stop(source) if self.state == "recording" else self.start(monitor, source)

    # -- workers
    def _fail(self, err, action, source, back_to="idle"):
        self.state = back_to
        self._say("error", map_error(err, action), source)

    def _do_start(self, monitor, source, idxs):
        try:
            r = self.rec.ensure_ready() or {}
            if not r.get("ok") and self.autolaunch and re.search(r"not running", (r.get("error") or "").lower()):
                r = self._launch_and_wait(r, source)
            if not r.get("ok"):
                return self._fail(r.get("error"), "start", source)
            labelled = []
            if not idxs:
                lm = self.rec.list_monitors() or {}
                if not lm.get("ok"):
                    return self._fail(lm.get("error") or "no monitor list", "start", source)
                labelled = label_monitors(lm.get("monitors"))
                if not labelled:
                    return self._fail("no monitors", "start", source)
                if len(labelled) == 1:
                    idxs = [labelled[0]["index"]]
                elif monitor is not None:
                    i = resolve_monitor(monitor, labelled)
                    if i is None:
                        self.state = "idle"
                        self._say("info", f"I can't match that to your {len(labelled)} screens.", source)
                        return self._ask(labelled, source)
                    idxs = [i]
                else:
                    self.state = "idle"
                    return self._ask(labelled, source)
            idxs = sorted(set(idxs), key=lambda i: next((m["ordinal"] for m in labelled if m["index"] == i), i))
            opts = dict(self._opts)
            r = (self.rec.start(monitor_index=idxs[0], **opts) if len(idxs) == 1
                 else self.rec.start(monitor_indexes=idxs, **opts)) or {}
            if not r.get("ok"):                              # the recorder only says ok once OBS confirms it is recording
                return self._fail(r.get("error"), "start", source)
            self.screens = list(idxs)
            if len(idxs) > 1:
                self.screen_name = "both screens" if len(idxs) == 2 else f"{len(idxs)} screens"
            else:
                self.screen_name = name_of(labelled, idxs[0]) if labelled else ""
            self.started_at, self.state = time.time(), "recording"
            self.audio = list(r["audio"]) if isinstance(r.get("audio"), list) else None
            self._say("info", f"Recording {self.screen_name or 'the screen'}{audio_phrase(self.audio)}.", source)
            for w in r.get("warnings") or []:
                self._say("info", str(w), source)
        except Exception as e:                       # the recorder never raises, but the UI must survive if it does
            self._fail(str(e), "start", source)

    def _launch_and_wait(self, r, source):
        """OBS is down: start it (start path only), poll until its websocket answers or launch_timeout passes.
        Returns the last ensure_ready-style dict."""
        launch = getattr(self.rec, "launch_obs", None)
        if not launch:
            return r
        l = launch() or {}
        if not l.get("ok"):
            return {"ok": False, "error": l.get("error") or "Could not launch OBS"}
        self._say("info", "Starting OBS, one moment.", source)
        deadline, last = time.time() + self.launch_timeout, r
        while True:
            time.sleep(self.poll_interval)
            last = self.rec.ensure_ready() or {}
            if last.get("ok"):
                return last
            low = (last.get("error") or "").lower()
            if re.search(r"password|auth", low) or time.time() >= deadline:
                return last
        return last

    def _ask(self, labelled, source):
        if self.choose:
            try:
                self.choose(labelled, source)
            except Exception as e:
                self._say("error", f"I couldn't show the screen chooser: {e}", source)
        else:
            self._say("info", "Which screen? " + ", ".join(m["desc"] for m in labelled), source)

    def _do_stop(self, source):
        try:
            r = self.rec.stop() or {}
            if not r.get("ok") and re.search(r"not recording", (r.get("error") or "").lower()):
                self.started_at, self.state = None, "idle"       # OBS isn't recording (it died or was stopped in OBS)
                return self._say("info", "Not recording.", source)
            if not r.get("ok"):
                return self._fail(r.get("error"), "stop", source, back_to="recording")
            self.last_paths = list(r.get("paths") or ([r["path"]] if r.get("path") else []))
            self.last_path, self.started_at, self.state = r.get("path") or None, None, "idle"
            where = _short_path(self.last_path)
            full = os.path.normpath(self.last_path) if self.last_path else ""
            self._say("info", f"Stopped, saved to {full}" if full else "Stopped.", source,
                      spoken=f"Stopped, saved to {where}." if where else "Stopped.")
        except Exception as e:
            self._fail(str(e), "stop", source, back_to="recording")
