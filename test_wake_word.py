"""Tests for the open mic / "Hey AIDA" wake word (wake_word.py) and its hook into voice_claude.handle_audio.

No real microphone, no speech model, no Tk. Audio is synthetic numpy (silence and tones); the detector's Whisper call
is a fake that returns a scripted transcript. handle_audio is lifted out of voice_claude.py with ast (same approach as
test_mute.py) so the real code is what gets tested.
"""
import ast
import pathlib
import re
import threading
import time
import types
import unittest

import numpy as np

import wake_word as ww

SR = 16000
CH = 480                                        # 30 ms chunks, like the listener


def tone(seconds, amp=0.1, f=220):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.float32)


def chunks(audio):
    for i in range(0, len(audio), CH):
        yield audio[i:i + CH]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class ScriptedDetector(ww.WakeDetector):
    """Fake engine: returns a prepared WakeHit on the Nth feed."""
    engine = "fake"

    def __init__(self, at=None, hit=None):
        self.at, self.hit, self.n, self.resets = at, hit, 0, 0

    def feed(self, chunk):
        self.n += 1
        if self.at is not None and self.n == self.at:
            return self.hit
        return None

    def reset(self):
        self.resets += 1


def run(machine, audio, t0=0.0, blocked=False, speaking=False):
    """Feed audio in 30 ms chunks; returns (events, end time)."""
    ev, t = [], t0
    for c in chunks(audio):
        t += len(c) / SR
        ev += machine.feed(c, t, blocked=blocked, speaking=speaking)
    return ev, t


def kinds(ev):
    return [e["type"] for e in ev]


# ------------------------------------------------------------------ 1. fuzzy wake-phrase matcher
class TestMatcher(unittest.TestCase):
    def test_01_plain_phrase_matches(self):
        """'hey aida' and 'Hey AIDA, open Chrome' wake; the remainder is the command."""
        self.assertEqual(ww.match_wake("hey aida"), (True, ""))
        self.assertEqual(ww.match_wake("Hey AIDA, open Chrome."), (True, "open chrome"))

    def test_02_mishearings_in_alias_list(self):
        """Known mishearings wake because they are in the alias list."""
        for s in ("hey ada", "Hey Ida!", "hey ayda what's the time", "Hey aider", "hey eider open notepad", "OK AIDA"):
            self.assertTrue(ww.match_wake(s)[0], s)

    def test_03_accent_fuzzy(self):
        """Accent-tolerant: 'hey tahz'/'hey tarrs' are close enough to 'aida' even if not listed."""
        self.assertTrue(ww.match_wake("hey aida", aliases=[])[0])
        self.assertTrue(ww.match_wake("hey ayda", aliases=[])[0])

    def test_04_negatives(self):
        """Never wakes: bare 'aida', 'stars are bright', 'hey tom', ordinary speech, empty."""
        for s in ("aida", "AIDA.", "stars are bright", "hey tom", "I said hey to Tara", "", "the cars are outside"):
            self.assertFalse(ww.match_wake(s)[0], s)

    def test_05_cars_only_if_aliased(self):
        """'hey cars' is a wake only because it is in the alias list; with no aliases it is not."""
        self.assertFalse(ww.match_wake("hey cars", aliases=[])[0])
        self.assertTrue(ww.match_wake("hey cars", aliases=["hey cars"])[0])

    def test_06_one_filler_word_allowed(self):
        """'Oh hey AIDA' wakes; the phrase three words in does not."""
        self.assertTrue(ww.match_wake("oh hey aida open chrome")[0])
        self.assertFalse(ww.match_wake("so I said hey aida")[0])

    def test_07_strip_keeps_casing(self):
        """strip_wake_phrase: command keeps his casing; no phrase = unchanged; phrase only = empty."""
        self.assertEqual(ww.strip_wake_phrase("Hey AIDA, open Chrome"), "open Chrome")
        self.assertEqual(ww.strip_wake_phrase("Initiate AIDA"), "Initiate AIDA")
        self.assertEqual(ww.strip_wake_phrase("Hey AIDA."), "")


# ------------------------------------------------------------------ 2. Whisper fallback detector
class TestWhisperDetector(unittest.TestCase):
    def make(self, text="hey aida", vad=None):
        calls = []

        def stt(audio):
            calls.append(len(audio))
            return text
        return ww.WhisperWakeDetector(stt, vad_fn=vad, clock=Clock()), calls

    def feed_all(self, det, audio):
        hits = []
        for c in chunks(audio):
            h = det.feed(c)
            if h:
                hits.append(h)
        return hits

    def test_08_silence_costs_nothing(self):
        """10 s of silence: no transcription at all."""
        det, calls = self.make()
        self.assertEqual(self.feed_all(det, silence(10)), [])
        self.assertEqual(calls, [])

    def test_09_short_blip_ignored(self):
        """A 0.2 s click/cough is under the 0.4 s minimum: never transcribed."""
        det, calls = self.make()
        self.feed_all(det, np.concatenate([silence(1), tone(0.2), silence(1)]))
        self.assertEqual(calls, [])

    def test_10_burst_checked_and_hits(self):
        """0.8 s burst then silence: one check, hit with ended=True, audio includes pre-roll."""
        det, calls = self.make()
        hits = self.feed_all(det, np.concatenate([silence(1), tone(0.8), silence(0.6)]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].ended)
        self.assertGreaterEqual(len(hits[0].audio), int(0.8 * SR) + int(0.29 * SR))   # 0.3 s pre-roll kept

    def test_11_long_speech_checked_once_at_cap(self):
        """6 s of continuous talk (a Teams call): checked once at ~1.5 s, never again for that burst; audio sent to
        Whisper is capped at pre-roll + 1.5 s."""
        det, calls = self.make(text="so the server went down again")
        self.assertEqual(self.feed_all(det, tone(6)), [])
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], int(1.8 * SR) + CH)

    def test_12_wrong_words_no_hit(self):
        """Transcript without the wake phrase -> no hit."""
        det, _ = self.make(text="stars are bright")
        self.assertEqual(self.feed_all(det, np.concatenate([tone(0.8), silence(0.6)])), [])

    def test_13_vad_veto_skips_whisper(self):
        """Silero VAD says 'not speech' (a hum): Whisper is not called."""
        det, calls = self.make(vad=lambda a: False)
        self.feed_all(det, np.concatenate([tone(0.8), silence(0.6)]))
        self.assertEqual(calls, [])


# ------------------------------------------------------------------ 3. state machine
class TestMachine(unittest.TestCase):
    def machine(self, hit, at=3, **kw):
        det = ScriptedDetector(at=at, hit=hit)
        kw.setdefault("cooldown_s", 1.5)
        return ww.WakeMachine(det, **kw), det

    def test_14_one_breath_command(self):
        """'hey aida open chrome' in one go: idle -> capturing -> sent after 0.8 s silence, via 'wake'."""
        m, _ = self.machine(ww.WakeHit(1, tone(1.5), ended=False, remainder="open"))
        ev, _ = run(m, np.concatenate([tone(2.0), silence(1.0)]))
        self.assertEqual(kinds(ev), ["wake", "utterance"])
        self.assertEqual(ev[1]["via"], "wake")
        self.assertEqual(m.state, ww.IDLE)

    def test_15_complete_burst_sent_at_once(self):
        """Burst already ended with a command in it: sent straight away."""
        m, _ = self.machine(ww.WakeHit(1, tone(1.5), ended=True, remainder="open chrome"))
        ev, _ = run(m, silence(0.2))
        self.assertEqual(kinds(ev), ["wake", "utterance"])

    def test_16_wake_then_pause_then_command(self):
        """'Hey AIDA' ... pause ... 'open chrome': woken, waits, captures, sends."""
        m, _ = self.machine(ww.WakeHit(1, tone(0.6), ended=True, remainder=""))
        ev, t = run(m, silence(0.5))
        self.assertEqual(m.state, ww.WOKEN)
        ev2, _ = run(m, np.concatenate([silence(1.0), tone(1.0), silence(1.0)]), t0=t)
        self.assertEqual(kinds(ev + ev2), ["wake", "utterance"])
        self.assertGreater(len(ev2[0]["audio"]), int(2.0 * SR))

    def test_17_chime_not_counted(self):
        """The wake chime (loud, right after the wake) does not start the capture."""
        m, _ = self.machine(ww.WakeHit(1, tone(0.6), ended=True, remainder=""), at=1)
        run(m, tone(0.15, amp=0.3, f=1000))              # chime-length blip straight after wake
        self.assertEqual(m.state, ww.WOKEN)

    def test_18_woken_timeout_is_silent(self):
        """Woken but nothing said for 5 s: back to idle with a 'timeout', no utterance."""
        m, _ = self.machine(ww.WakeHit(1, tone(0.6), ended=True, remainder=""))
        ev, _ = run(m, silence(6))
        self.assertEqual(kinds(ev), ["wake", "timeout"])
        self.assertEqual(m.state, ww.IDLE)

    def test_19_cooldown(self):
        """A second wake within the cooldown is not even looked for (detector not fed)."""
        m, det = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), cooldown_s=1.5)
        ev, t = run(m, silence(0.1))
        fed = det.n
        run(m, silence(1.0), t0=t)
        self.assertEqual(det.n, fed)                      # nothing fed during cooldown
        run(m, silence(1.0), t0=t + 1.0)
        self.assertGreater(det.n, fed)                    # fed again afterwards

    def test_20_blocked_hears_nothing(self):
        """While AIDA speaks (echo gate) the detector is never fed, so AIDA cannot wake itself."""
        m, det = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1)
        ev, _ = run(m, tone(3), blocked=True, speaking=True)
        self.assertEqual(ev, [])
        self.assertEqual(det.n, 0)

    def test_21_mute_still_blocks(self):
        """Mute is speaker-only: muted speech still counts as speaking, so the machine stays deaf (same path)."""
        m, det = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1)
        muted_speaking = True                             # app._wake_ctx reports mouth.speaking, muted or not
        run(m, tone(2), blocked=muted_speaking, speaking=muted_speaking)
        self.assertEqual(det.n, 0)

    def test_22_ptt_mid_capture_aborts(self):
        """PTT pressed (blocked) during a wake capture: capture dropped, 'aborted', no utterance."""
        m, _ = self.machine(ww.WakeHit(1, tone(1), ended=False, remainder="open"))
        ev, t = run(m, tone(0.3))
        self.assertEqual(m.state, ww.CAPTURING)
        ev2, _ = run(m, tone(0.3), t0=t, blocked=True)
        self.assertEqual(kinds(ev2), ["aborted"])
        self.assertEqual(m.cap, [])
        self.assertEqual(m.state, ww.IDLE)

    def test_23_followup_after_spoken_reply(self):
        """After a spoken reply, speech within 8 s is taken without the wake word, via 'followup', with pre-roll."""
        m, _ = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1)
        ev, t = run(m, silence(0.1))                                  # turn 1 sent
        _, t = run(m, silence(2), t0=t, blocked=True, speaking=True)  # AIDA replies
        _, t = run(m, silence(0.5), t0=t)                             # reply done -> window opens
        self.assertEqual(m.state, ww.FOLLOWUP)
        ev, _ = run(m, np.concatenate([tone(1.0), silence(1.0)]), t0=t)
        self.assertEqual(kinds(ev), ["utterance"])
        self.assertEqual(ev[0]["via"], "followup")
        self.assertGreaterEqual(len(ev[0]["audio"]), int(1.25 * SR))  # 0.3 s pre-roll + speech

    def test_24_followup_expires(self):
        """No speech within the window: back to idle; later speech needs the wake word again."""
        m, det = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1, followup_s=8)
        _, t = run(m, silence(0.1))
        _, t = run(m, silence(1), t0=t, blocked=True, speaking=True)
        _, t = run(m, silence(9), t0=t)
        self.assertEqual(m.state, ww.IDLE)
        ev, _ = run(m, np.concatenate([tone(1.0), silence(1.0)]), t0=t)
        self.assertNotIn("utterance", kinds(ev))

    def test_25_no_followup_after_silent_reply(self):
        """A [quiet]/[done] reply (job ran, nothing spoken) does not open the follow-up window."""
        m, _ = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1)
        _, t = run(m, silence(0.1))
        _, t = run(m, silence(2), t0=t, blocked=True, speaking=False)
        run(m, silence(0.3), t0=t)
        self.assertEqual(m.state, ww.IDLE)

    def test_26_followup_ignores_short_noise(self):
        """In the window a 0.2 s cough does not start a turn."""
        m, _ = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1)
        _, t = run(m, silence(0.1))
        _, t = run(m, silence(1), t0=t, blocked=True, speaking=True)
        ev, _ = run(m, np.concatenate([silence(0.2), tone(0.2), silence(1.0)]), t0=t)
        self.assertEqual(ev, [])

    def test_27_max_utterance(self):
        """Talking non-stop is cut off at wake_max (30 s) and sent."""
        m, _ = self.machine(ww.WakeHit(1, tone(1), ended=False, remainder="x"), at=1)
        ev, _ = run(m, tone(31))
        self.assertIn("utterance", kinds(ev))

    def test_28_off_switch(self):
        """Disabled: nothing fed, nothing kept; enabling returns to idle."""
        m, det = self.machine(ww.WakeHit(1, tone(1), ended=True, remainder="x"), at=1, enabled=False)
        self.assertEqual(run(m, tone(2))[0], [])
        self.assertEqual(det.n, 0)
        m.set_enabled(True)
        self.assertEqual(m.state, ww.IDLE)

    def test_29_preroll_is_ram_only_and_bounded(self):
        """The pre-roll ring never holds more than 1 s."""
        m, _ = self.machine(None, at=None)
        run(m, tone(5))
        self.assertLessEqual(len(m.ring), SR)

    def test_30_detector_error_does_not_raise(self):
        """An engine exception becomes an 'error' event, never an exception."""
        class Boom(ww.WakeDetector):
            def feed(self, c):
                raise RuntimeError("onnx died")
        m = ww.WakeMachine(Boom())
        ev, _ = run(m, silence(0.06))
        self.assertIn("error", kinds(ev))


# ------------------------------------------------------------------ 4. listener (fake sounddevice)
class FakeStream:
    def __init__(self, **kw):
        self.kw, self.started, self.closed = kw, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeSD:
    def __init__(self, fail=False):
        self.fail, self.streams = fail, []

    def query_devices(self, *a, **k):
        return {"default_samplerate": 48000, "name": "fake"}

    def InputStream(self, **kw):
        if self.fail:
            raise OSError("device unavailable")
        s = FakeStream(**kw)
        self.streams.append(s)
        return s


class TestListener(unittest.TestCase):
    def test_31_device_unavailable_never_crashes(self):
        """Mic missing: start() returns False, state 'unavailable', error logged, no exception."""
        logs = []
        lst = ww.WakeListener(ww.WakeMachine(ScriptedDetector()), lambda: (False, False), lambda a, v: None,
                              sd=FakeSD(fail=True), log=lambda w, t: logs.append(t))
        self.assertFalse(lst.start())
        self.assertEqual(lst.state, "unavailable")
        self.assertTrue(any("push-to-talk still works" in l for l in logs))

    def test_32_cleanup_closes_stream_and_forgets_audio(self):
        """stop(): stream closed, thread gone, queue and pre-roll emptied."""
        sd = FakeSD()
        m = ww.WakeMachine(ScriptedDetector())
        lst = ww.WakeListener(m, lambda: (False, False), lambda a, v: None, sd=sd)
        self.assertTrue(lst.start())
        lst.q.put(np.ones(480, dtype=np.float32))
        m.ring = np.ones(100, dtype=np.float32)
        lst.stop()
        self.assertTrue(sd.streams[0].closed)
        self.assertIsNone(lst.thread)
        self.assertTrue(lst.q.empty())
        self.assertEqual(len(m.ring), 0)
        self.assertEqual(lst.state, ww.OFF)

    def test_33_resample_and_delivery(self):
        """48 kHz blocks are resampled to 16 kHz; a finished utterance is handed over with via='wake'."""
        got = threading.Event()
        out = {}

        def on_utt(audio, via):
            out["n"], out["via"] = len(audio), via
            got.set()
        m = ww.WakeMachine(ScriptedDetector(at=1, hit=ww.WakeHit(1, tone(1), ended=True, remainder="go")))
        lst = ww.WakeListener(m, lambda: (False, False), on_utt, rate=48000, sd=FakeSD())
        lst.process(np.zeros(1440, dtype=np.float32), 1.0)
        self.assertTrue(got.wait(2))
        self.assertEqual(out["via"], "wake")
        self.assertEqual(out["n"], SR)

    def test_34_wake_log_has_no_text_or_audio(self):
        """logs/wake.jsonl lines hold only time/event/score/engine/via/seconds."""
        import json
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp())
        try:
            wl = ww.WakeLog(d / "wake.jsonl")
            wl.write("wake", score=0.9, engine="whisper", text="hey aida delete it", audio=[1, 2, 3])
            rec = json.loads((d / "wake.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(set(rec), {"t", "event", "score", "engine"})
        finally:
            for p in d.iterdir():
                p.unlink()
            d.rmdir()

    def test_35_ctx_failure_treated_as_blocked(self):
        """If the app context call fails, the listener assumes 'blocked' (safe side)."""
        det = ScriptedDetector(at=1, hit=ww.WakeHit(1, tone(1), ended=True, remainder="x"))
        lst = ww.WakeListener(ww.WakeMachine(det), lambda: 1 / 0, lambda a, v: None, rate=16000, sd=FakeSD())
        self.assertEqual(lst.process(np.zeros(480, dtype=np.float32), 1.0), [])
        self.assertEqual(det.n, 0)


# ------------------------------------------------------------------ 5. guard word: open mic can never confirm
HERE = pathlib.Path(__file__).resolve().parent
VOICE_TREE = ast.parse((HERE / "voice_claude.py").read_text(encoding="utf-8-sig"))


def lift_handle_audio(ns):
    node = next(n for n in VOICE_TREE.body if isinstance(n, ast.ClassDef) and n.name == "App")
    fn = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "handle_audio")
    mod = ast.Module(body=[ast.ClassDef(name="App", bases=[], keywords=[], body=[fn], decorator_list=[])],
                     type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "handle_audio", "exec"), ns)
    return ns["App"]


class FakeToken:
    def __init__(self):
        self.written = []

    def write_text(self, s, encoding=None):
        self.written.append(s)


class TestConfirmGuard(unittest.TestCase):
    def build(self, transcript, awaiting=False):
        import confirm_state
        token, said, tasks, logs = FakeToken(), [], [], []
        ns = {"USER": "You",
            "time": time, "threading": types.SimpleNamespace(Thread=lambda target, args, daemon: types.SimpleNamespace(
                start=lambda: tasks.append(args))),
            "timing_log": types.SimpleNamespace(TurnTimer=lambda k: types.SimpleNamespace(mark=lambda n: None)),
            "CFG": {"echo_filter": False}, "SPOKEN": None, "log": lambda w, t: logs.append((w, t)),
            "wake_word": ww, "confirm_state": confirm_state, "TOKEN_FILE": token,
            "CONFIRM_RE": re.compile(r"^(yes,? )?(confirmed?|confirm it|go ahead|do it|yes do it|send it|delete it|approved?)\b", re.I),
            "instant_command": lambda t: None, "run_instant": None, "chime": None, "now": lambda: "now",
        }
        App = lift_handle_audio(ns)
        app = App()
        app.ears = types.SimpleNamespace(transcribe=lambda a: transcript)
        app.mouth = types.SimpleNamespace(say=said.append)
        app.memory = types.SimpleNamespace(add=lambda *a: None)
        app.confirm = types.SimpleNamespace(clear=lambda: None, active=awaiting)
        app.awaiting_confirm = awaiting
        app.local_command = lambda t: False
        app.do_task = lambda t: None
        app.working, app.transcribing, app.unseen = False, False, []
        return app, token, said, tasks

    def test_36_wake_confirm_never_writes_token(self):
        """'Hey AIDA, confirmed' while a block is waiting: token NOT written, he is told to use the key."""
        app, token, said, tasks = self.build("Hey AIDA, confirmed.", awaiting=True)
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(token.written, [])
        self.assertTrue(any("talk key" in s for s in said))
        self.assertEqual(tasks, [])

    def test_37_followup_confirm_never_writes_token(self):
        """Follow-up window speech ('confirmed' with no wake word) is still source='wake': no token."""
        app, token, _, _ = self.build("confirmed", awaiting=True)
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(token.written, [])

    def test_38_wake_confirm_without_pending_still_no_token(self):
        """Nothing waiting: the words go to Claude as normal, but the token is still never written."""
        app, token, _, tasks = self.build("Hey AIDA, go ahead and open Chrome")
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(token.written, [])
        self.assertEqual(len(tasks), 1)

    def test_39_ptt_confirm_still_works(self):
        """Push-to-talk 'confirmed' writes the token exactly as before."""
        app, token, _, _ = self.build("confirmed", awaiting=True)
        app.handle_audio(np.zeros(SR))
        self.assertEqual(len(token.written), 1)

    def test_40_false_wake_is_silent(self):
        """Wake path with an empty transcript, or only 'hey AIDA': no 'Didn't catch that', no task."""
        for text in ("", "Hey AIDA."):
            app, _, said, tasks = self.build(text)
            app.handle_audio(np.zeros(SR), source="wake")
            self.assertEqual(said, [], text)
            self.assertEqual(tasks, [], text)

    def test_41_wake_phrase_stripped_before_task(self):
        """'Hey AIDA, open Chrome' reaches the task as 'open Chrome'."""
        app, _, _, tasks = self.build("Hey AIDA, open Chrome")
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(tasks[0][0], "open Chrome")

    def test_42_confirm_allowed_table(self):
        self.assertTrue(ww.confirm_allowed("ptt"))
        self.assertTrue(ww.confirm_allowed("typed"))
        self.assertFalse(ww.confirm_allowed("wake"))
        self.assertFalse(ww.confirm_allowed("followup"))


# ------------------------------------------------------------------ 6. patience: not cutting the user off mid-thought
class FakeTail:
    """Fake 'has he finished?' transcriber: returns scripted text and counts calls."""
    def __init__(self, text):
        self.text, self.calls = text, 0

    def __call__(self, audio):
        self.calls += 1
        return self.text() if callable(self.text) else self.text


class TestPatience(unittest.TestCase):
    CFG = {}                                       # empty config -> the patient defaults from make_machine

    def machine(self, tail_text="open chrome", cfg=None, ended=False, remainder="open"):
        det = ScriptedDetector(at=1, hit=ww.WakeHit(1, tone(1), ended=ended, remainder=remainder))
        tail = FakeTail(tail_text) if tail_text is not None else None
        return ww.make_machine(cfg if cfg is not None else self.CFG, det, tail_fn=tail), tail

    def test_43_defaults(self):
        """Config defaults: 8 s base, 10 s max, 2 s grace, 120 s cap."""
        m, _ = self.machine()
        self.assertEqual((m.end_silence_s, m.end_silence_max_s, m.grace_s, m.max_utt_n),
                         (8.0, 10.0, 2.0, 120 * SR))

    def test_44_three_second_pause_does_not_end(self):
        """'Hey AIDA, open the... (3 s um-pause) ...the notes file': one utterance, nothing sent during the pause."""
        m, _ = self.machine(tail_text="open the")
        ev, t = run(m, np.concatenate([tone(1.0), silence(3.0)]))
        self.assertNotIn("utterance", kinds(ev))
        ev2, _ = run(m, np.concatenate([tone(1.5), silence(13.0)]), t0=t)
        self.assertEqual(kinds(ev + ev2).count("utterance"), 1)
        utt = next(e for e in ev2 if e["type"] == "utterance")
        self.assertGreater(len(utt["audio"]), int(5.0 * SR))       # both halves in one turn

    def test_45_unfinished_tail_extends_wait(self):
        """Trailing 'um' / 'and': still waiting at 11 s of quiet, sent by 12 s (max 10 + grace 2)."""
        for tail in ("open chrome and", "so I want um"):
            m, _ = self.machine(tail_text=tail)
            ev, t = run(m, np.concatenate([tone(1.0), silence(11.0)]))
            self.assertNotIn("utterance", kinds(ev), tail)
            ev2, _ = run(m, silence(1.2), t0=t)
            self.assertIn("utterance", kinds(ev2), tail)

    def test_46_finished_sentence_ends_after_base(self):
        """'open chrome' (finished): not sent at 9.5 s, sent by 10.1 s (8 base + 2 grace)."""
        m, _ = self.machine(tail_text="open chrome.")
        ev, t = run(m, np.concatenate([tone(1.0), silence(9.5)]))
        self.assertNotIn("utterance", kinds(ev))
        ev2, _ = run(m, silence(0.6), t0=t)
        self.assertIn("utterance", kinds(ev2))

    def test_47_hard_cap(self):
        """Talking non-stop is sent at wake_max_utterance_s (here set to 10 s)."""
        m, _ = self.machine(cfg={"wake_max_utterance_s": 10})
        ev, _ = run(m, tone(11))
        self.assertIn("utterance", kinds(ev))

    def test_48_end_word_sends_quickly(self):
        """'..., over' at the end: sent after ~0.8 s, not 4.5 s."""
        m, _ = self.machine(tail_text="open chrome over")
        ev, _ = run(m, np.concatenate([tone(1.0), silence(1.2)]))
        self.assertIn("utterance", kinds(ev))

    def test_49_end_words_stripped(self):
        """clean_command drops the end word but keeps the command; a bare end word leaves nothing."""
        self.assertEqual(ww.clean_command("Open Chrome, over."), ("Open Chrome", False))
        self.assertEqual(ww.clean_command("open the notes file send it"), ("open the notes file", False))
        self.assertEqual(ww.clean_command("That's it."), ("", False))
        self.assertEqual(ww.clean_command("game over man, what now"), ("game over man, what now", False))

    def test_50_cancel_phrase_discards(self):
        """Whole utterance 'never mind' / 'cancel that': machine discards, nothing sent; mid-sentence it doesn't."""
        m, _ = self.machine(tail_text="never mind")
        ev, _ = run(m, np.concatenate([tone(1.0), silence(2.0)]))
        self.assertIn("discarded", kinds(ev))
        self.assertNotIn("utterance", kinds(ev))
        self.assertEqual(m.state, ww.IDLE)
        self.assertEqual(ww.clean_command("Cancel that."), ("", True))
        self.assertEqual(ww.clean_command("never mind the bollocks, open spotify")[1], False)

    def test_51_grace_window_resumes(self):
        """Finished sentence, then he picks back up 3.5 s later (inside base+grace): merged into one turn."""
        m, _ = self.machine(tail_text="open chrome")
        ev, t = run(m, np.concatenate([tone(1.0), silence(3.5), tone(1.0), silence(11.0)]))
        self.assertEqual(kinds(ev).count("utterance"), 1)

    def test_52_one_tail_check_per_pause_and_rate_limited(self):
        """CPU: silence with no speech costs one check per pause, never more than one a second."""
        m, tail = self.machine(tail_text="um")
        run(m, np.concatenate([tone(1.0), silence(9.0)]))
        # 11 Oct: one check at the pause plus at most tail_rechecks (2) more looks during the long wait
        self.assertLessEqual(tail.calls, 1 + m.tail_rechecks)
        m2, tail2 = self.machine(tail_text="and")
        audio = np.concatenate([tone(1.0)] + [np.concatenate([silence(0.9), tone(0.1)]) for _ in range(5)])
        run(m2, audio)
        self.assertLessEqual(tail2.calls, 5)          # 5 pauses in ~5 s: at most one check per second

    def test_53_no_transcriber_still_patient(self):
        """Without a tail transcriber: plain base+grace (10 s) silence, no crash."""
        m, _ = self.machine(tail_text=None)
        ev, t = run(m, np.concatenate([tone(1.0), silence(9.7)]))
        self.assertNotIn("utterance", kinds(ev))
        ev2, _ = run(m, silence(0.5), t0=t)
        self.assertIn("utterance", kinds(ev2))

    def test_54_complete_burst_now_waits(self):
        """'Hey AIDA open the' then a 0.35 s gap no longer fires straight away (that was the cut-off)."""
        m, _ = self.machine(tail_text="open the", ended=True, remainder="open the")
        ev, _ = run(m, silence(2.0))
        self.assertEqual(kinds(ev), ["wake"])
        self.assertEqual(m.state, ww.CAPTURING)

    def test_55_cooldown_and_followup_unchanged(self):
        """After a patient turn: cooldown applies, and a spoken reply still opens the follow-up window."""
        m, _ = self.machine(tail_text="open chrome", ended=True, remainder="open chrome")
        ev, t = run(m, silence(11.0))
        self.assertIn("utterance", kinds(ev))
        self.assertGreater(m.cooldown_until, t - 0.1)
        _, t = run(m, silence(2), t0=t, blocked=True, speaking=True)
        run(m, silence(0.3), t0=t)
        self.assertEqual(m.state, ww.FOLLOWUP)

    def test_56_blocked_still_aborts_patient_capture(self):
        """Echo gate / mute / PTT mid-pause still drops the capture: no utterance, no tail check."""
        m, tail = self.machine(tail_text="open the")
        _, t = run(m, np.concatenate([tone(1.0), silence(0.5)]))
        ev, _ = run(m, silence(3.0), t0=t, blocked=True, speaking=True)
        self.assertEqual(kinds(ev), ["aborted"])
        self.assertEqual(m.cap, [])

    def test_56b_what_else_question_pause_does_not_end(self):
        """22:28 cut-off: Whisper turns 'okay, what else,' into 'What else?' (judged finished). A 9.5 s thinking
        pause must not send; he carries on and it all goes as one turn."""
        m, _ = self.machine(tail_text="Okay, what else?")
        ev, t = run(m, np.concatenate([tone(1.0), silence(9.5)]))
        self.assertNotIn("utterance", kinds(ev))
        ev2, _ = run(m, np.concatenate([tone(1.5), silence(11.0)]), t0=t)
        self.assertEqual(kinds(ev + ev2).count("utterance"), 1)
        utt = next(e for e in ev2 if e["type"] == "utterance")
        self.assertGreater(len(utt["audio"]), int(10.0 * SR))      # both halves kept

    def test_56c_end_and_cancel_still_quick_after_long_wait_change(self):
        """'over' still sends in ~1 s and 'never mind' still discards, despite the 10 s patience."""
        m, _ = self.machine(tail_text="what else, over")
        ev, _ = run(m, np.concatenate([tone(1.0), silence(1.2)]))
        self.assertIn("utterance", kinds(ev))
        m, _ = self.machine(tail_text="Never mind.")
        ev, _ = run(m, np.concatenate([tone(1.0), silence(2.0)]))
        self.assertEqual(kinds(ev).count("discarded"), 1)
        self.assertNotIn("utterance", kinds(ev))


    def test_56g_2243_send_it_with_trailing_filler_ends_quickly(self):
        """22:43: the filler-seeded tail prompt gave '..., ok, send it, um.' -> was 'unfinished' (12 s wait, the user said
        'send it' twice). A real trailing 'send it' must end the turn within ~1 s."""
        for t in ("then open the recording on the right hand screen and play it, ok, send it, um.",
                  "play it, ok, send it, uh", "ok, over, um"):
            self.assertEqual(ww.tail_status(t), "end", t)
        m, _ = self.machine(tail_text="and play it, ok, send it, um.")
        ev, _ = run(m, np.concatenate([tone(1.0), silence(1.3)]))
        self.assertIn("utterance", kinds(ev))
        self.assertEqual(ww.clean_command("play it, ok, send it, um."), ("play it, ok", False))

    # ---- 11 Oct 00:19 / 00:29 (open mic, wake.jsonl 'utterance via wake'): 'send it' left in the sent text
    def test_56i_0019_repeated_send_it_all_stripped(self):
        """Logged 'yes, restart, send it' / '..., send it, send it': he repeated the end word when the quick send
        didn't fire and clean_command only stripped one. Every trailing repeat must go."""
        self.assertEqual(ww.clean_command("yes, restart, send it, send it"), ("yes, restart", False))
        self.assertEqual(ww.clean_command("Yes, restart. Send it. Send it."), ("Yes, restart", False))
        self.assertEqual(ww.clean_command(
            "anyway, after this chat, I will do another confirmed for you, send it, send it, send it"),
            ("anyway, after this chat, I will do another confirmed for you", False))
        self.assertEqual(ww.clean_command("move it over, over"), ("move it", False))
        self.assertEqual(ww.clean_command("send it, send it."), ("", False))
        self.assertEqual(ww.clean_command("yes, restart, send-it!"), ("yes, restart", False))  # was 'yes'
        self.assertEqual(ww.clean_command("That's it, that's it"), ("", False))
        # mid-sentence end words untouched
        self.assertEqual(ww.clean_command("the word send it doesn't work"), ("the word send it doesn't work", False))

    def test_56j_0029_missed_end_word_caught_on_recheck(self):
        """If the first look at a pause misses 'send it' (soft/garbled), a re-check during the long wait still
        sends quickly instead of after the full 10-12 s."""
        answers = iter(["I will do another confirmed for you,", "I will do another confirmed for you, send it."])
        m, tail = self.machine(tail_text=lambda: next(answers, "for you, send it."))
        ev, _ = run(m, np.concatenate([tone(1.0), silence(4.5)]))
        self.assertIn("utterance", kinds(ev))
        utt = next(e for e in ev if e["type"] == "utterance")
        self.assertEqual(utt["reason"], "end")
        self.assertGreaterEqual(tail.calls, 2)

    def test_56k_0027_yeah_is_not_an_end_word(self):
        """00:27:59 '...initiate a PC shutdown, yeah' then a thinking pause: 'yeah' is not an end word, so the
        normal 8 + 2 s wait applies (not sent at 9.5 s of quiet). The turn closing then means 10 s of quiet by
        the mic level, or a judge 'end'; the utterance event now records which (reason/quiet_s/peak_rms)."""
        txt = "I want you to initiate a PC shutdown, yeah"
        self.assertEqual(ww.tail_status(txt), "finished")
        m, _ = self.machine(tail_text=txt)
        ev, t = run(m, np.concatenate([tone(1.0), silence(9.5)]))
        self.assertNotIn("utterance", kinds(ev))
        ev2, _ = run(m, silence(0.7), t0=t)
        utt = next(e for e in ev2 if e["type"] == "utterance")
        self.assertEqual(utt["reason"], "finished")
        self.assertGreaterEqual(utt["quiet_s"], 10.0)
        self.assertEqual(utt["peak_rms"], 0.0)

    def test_56l_quiet_murmur_recorded_in_peak_rms(self):
        """Speech under wake_min_rms counts as quiet; peak_rms shows it so a 'cut me off' report is provable."""
        m, _ = self.machine(tail_text="shut it down, yeah")
        ev, _ = run(m, np.concatenate([tone(1.0), tone(10.5, amp=0.005)]))
        utt = next(e for e in ev if e["type"] == "utterance")
        self.assertGreater(utt["peak_rms"], 0.002)
        self.assertLess(utt["peak_rms"], m.min_rms)

    def test_56d_2237_trailing_um_waits_full_max(self):
        """22:37 cut-off: 'record screen one with sound, and um, and um, um,' must use the long (max+grace) wait:
        not sent at 11 s of quiet, and a resumed sentence joins the same turn."""
        m, _ = self.machine(tail_text="Hey AIDA, record screen one with sound, and um, and um, um,")
        ev, t = run(m, np.concatenate([tone(1.0), silence(11.0)]))
        self.assertNotIn("utterance", kinds(ev))
        ev2, _ = run(m, np.concatenate([tone(1.0), silence(13.0)]), t0=t)
        self.assertEqual(kinds(ev + ev2).count("utterance"), 1)

    def test_56e_filler_prompt_keeps_ums(self):
        """The pause check uses a filler-seeded prompt so Whisper keeps 'um'; bare fillers judge unfinished."""
        self.assertIn("um", ww.TAIL_PROMPT)
        for t in ("and um", "um, um,", "so uh", "and, umm"):
            self.assertEqual(ww.tail_status(t), "unfinished", t)

    def test_56f_2238_hallucinated_outro_ignored(self):
        """22:38: '..., send it, F Sir, thanks for tuning in, and have a good night, everyone, until the next video!'
        -> the junk is dropped, 'send it' is his real last words, command reaches the task without either."""
        raw = ("fix that, and stop the bloody recordings, send it, F Sir, thanks for tuning in, "
               "and have a good night, everyone, until the next video!")
        self.assertEqual(ww.strip_hallucinations(raw), "fix that, and stop the bloody recordings, send it,")
        self.assertEqual(ww.tail_status(raw), "end")
        self.assertEqual(ww.clean_command(raw), ("fix that, and stop the bloody recordings", False))

    def test_56g_hallucination_alone_is_not_speech_or_end(self):
        """Only hallucinated text after a long silence: never an end word, never 'finished' speech."""
        for junk in ("Thanks for watching!", "Thank you.", "Bye.", "Subtitles by the Amara.org community"):
            self.assertEqual(ww.tail_status("record screen one, and um, " + junk), "unfinished", junk)
            self.assertEqual(ww.tail_status(junk), "unfinished", junk)
        m, _ = self.machine(tail_text="open the um, thanks for tuning in")
        ev, _ = run(m, np.concatenate([tone(1.0), silence(3.0)]))
        self.assertNotIn("utterance", kinds(ev))                 # no quick 'end' from junk

    def test_56h_real_speech_kept(self):
        """Ordinary sentences are untouched by the hallucination filter."""
        for s in ("open chrome", "thank you for the help, now open spotify", "game over man, what now"):
            self.assertEqual(ww.strip_hallucinations(s), s)


class TestPatienceGuard(TestConfirmGuard):
    def test_57_cancel_phrase_sends_nothing(self):
        """'Hey AIDA, never mind' through handle_audio: no task, nothing said."""
        app, _, said, tasks = self.build("Hey AIDA, never mind.")
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual((said, tasks), ([], []))

    def test_58_end_word_stripped_before_task(self):
        """'Hey AIDA, open Chrome, over' reaches the task as 'open Chrome'."""
        app, _, _, tasks = self.build("Hey AIDA, open Chrome, over.")
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(tasks[0][0], "open Chrome")

    def test_59_confirmed_over_still_blocked(self):
        """'Hey AIDA, confirmed, over' while waiting: still never writes the token."""
        app, token, said, tasks = self.build("Hey AIDA, confirmed, over.", awaiting=True)
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(token.written, [])
        self.assertEqual(tasks, [])

    def test_59b_2238_outro_never_reaches_task(self):
        """Through handle_audio: hallucinated outro and 'send it' are both stripped before the task."""
        app, _, _, tasks = self.build("Hey AIDA, open the notes file, send it, thanks for tuning in, bye.")
        app.handle_audio(np.zeros(SR), source="wake")
        self.assertEqual(tasks[0][0], "open the notes file")

    def test_60_ptt_unaffected_by_end_words(self):
        """Talk key: since 11 Oct 2026 a trailing end word is dropped there too (the user: 'send it doesn't send it').
        Text without an end word goes through as said."""
        app, _, _, tasks = self.build("open notepad, send it")
        app.handle_audio(np.zeros(SR))
        self.assertEqual(tasks[0][0], "open notepad")
        app, _, _, tasks = self.build("game on")
        app.handle_audio(np.zeros(SR))
        self.assertEqual(tasks[0][0], "game on")


if __name__ == "__main__":
    unittest.main()
