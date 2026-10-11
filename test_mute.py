"""Tests for the global Mute switch. No real audio, no windows, no speech models.

voice_claude.py and chat_window.py are not imported whole (that would load models / Tk). Instead the few
functions under test are lifted out of the source with ast and run against fakes, so the real code is what is tested.
"""
import ast
import pathlib
import queue
import re
import threading
import time
import types
import unittest

import audio_mute
import chat_window as cw
import mute

MOUTH_LOG = []

HERE = pathlib.Path(__file__).resolve().parent
VOICE_SRC = (HERE / "voice_claude.py").read_text(encoding="utf-8-sig")
CHAT_SRC = (HERE / "chat_window.py").read_text(encoding="utf-8-sig")
VOICE_TREE, CHAT_TREE = ast.parse(VOICE_SRC), ast.parse(CHAT_SRC)


def lift(tree, cls, names, extra=None, bases=()):
    """Build a class from selected methods of `cls` in `tree`, executed in a namespace of fakes."""
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    fns = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {f.name for f in fns} == set(names), f"missing {set(names) - {f.name for f in fns}}"
    mod = ast.Module(body=[ast.ClassDef(name=cls, bases=[], keywords=[], body=fns, decorator_list=[])],
                     type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"time": time, "queue": queue, "re": re, "threading": threading}
    ns.update(extra or {})
    exec(compile(mod, cls, "exec"), ns)
    return ns[cls]


def lift_func(tree, name, extra=None):
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"re": re}
    ns.update(extra or {})
    exec(compile(mod, name, "exec"), ns)
    return ns[name]


# ------------------------------------------------------------------ a Mouth whose real methods run, minus the audio
class SpokenStub:
    def __init__(self):
        self.added = []

    def add(self, t):
        self.added.append(t)


class FakeVol:
    """One fake audio session (same shape as audio_mute._VolWrap): records every call."""

    def __init__(self, muted=False, volume=1.0):
        self.muted, self.volume, self.calls = muted, volume, []

    def set_mute(self, f):
        self.muted = bool(f)
        self.calls.append(("mute", bool(f)))

    def get_mute(self):
        return self.muted

    def set_volume(self, v):
        self.volume = v
        self.calls.append(("vol", round(v, 3)))

    def get_volume(self):
        return self.volume


class FakeBackend:
    def __init__(self, vols=None, fail=None):
        self.vols = [FakeVol()] if vols is None else vols
        self.fail = fail
        self.inits = 0

    def init_thread(self):
        self.inits += 1

    def sessions(self):
        if self.fail:
            raise self.fail
        return list(self.vols)


def make_muter(vols=None, **kw):
    be = FakeBackend(vols)
    return audio_mute.SessionMuter(backend=be, start=False, sleep=lambda s: None, **kw), be


class FakeMuter:
    """Stand-in for the module-level MUTER inside Mouth: records what Mouth asked for."""

    def __init__(self):
        self.calls = []

    def set(self, f, wait=False):
        self.calls.append(("set", bool(f)))

    def reapply(self, wait=False):
        self.calls.append(("reapply",))


def make_mouth():
    """The real Mouth.say / stop / set_muted, with the audio thread replaced by a fake 'playing' flag."""
    cls = lift(VOICE_TREE, "Mouth", ["say", "stop", "set_muted", "speaking"],
               {"is_marker": lift_func(VOICE_TREE, "is_marker"), "SPOKEN": SpokenStub(),
                "MUTER": FakeMuter(), "audio_mute": audio_mute, "log": lambda who, text: MOUTH_LOG.append((who, text))})
    # `speaking` is a @property in the source; lift() kept the decorator out, so rebuild it here
    cls.speaking = property(lambda self: self.busy or not self.q.empty())
    m = cls.__new__(cls)
    m.q = queue.Queue()
    m.stop_evt = threading.Event()
    m.busy = False
    m.muted = False
    m.muted_since, m.backlog_logged = 0.0, False
    m.hold = None                       # no speech hold in these tests (see test_speech_hold.py)
    MOUTH_LOG.clear()
    m.level, m.level_t = 0.0, 0.0
    m.text, m.text_t = "", 0.0
    return m


class TestMouthMute(unittest.TestCase):
    def test_mute_while_speaking_does_not_stop_playback_or_flush_queue(self):
        # NEW SEMANTICS: mid-sentence, one line playing, two queued. Mute must leave all of it running.
        m = make_mouth()
        m.say("first line being spoken")
        m.say("second line waiting")
        m.say("third line waiting")
        m.q.get()                               # the audio thread takes the first line
        m.busy, m.level, m.text = True, 0.4, "first line being spoken"
        m.set_muted(True)
        self.assertFalse(m.stop_evt.is_set(), "playback must NOT be told to stop")
        self.assertEqual(m.q.qsize(), 2, "queued replies stay queued and keep playing (silently)")
        self.assertEqual(m.level, 0.4, "head mouth keeps animating")
        self.assertEqual(m.text, "first line being spoken", "the face keeps its speaking cues")
        self.assertTrue(m.say.__globals__["MUTER"].calls[-1] == ("set", True), "the speaker session is silenced")

    def test_speech_while_muted_is_queued_not_dropped(self):
        m = make_mouth()
        m.set_muted(True)
        m.say("said while muted")
        self.assertEqual(m.q.get_nowait(), "said while muted", "it plays (silently) in order")
        self.assertEqual(m.say.__globals__["SPOKEN"].added, ["said while muted"], "echo log still sees it")

    def test_unmute_does_not_flush_or_replay(self):
        m = make_mouth()
        m.say("old line")
        m.set_muted(True)
        m.say("said while muted")
        m.set_muted(False)
        self.assertEqual(m.q.qsize(), 2, "nothing dropped, nothing re-queued")
        self.assertEqual([m.q.get_nowait(), m.q.get_nowait()], ["old line", "said while muted"])
        self.assertFalse(m.stop_evt.is_set())
        self.assertEqual(m.say.__globals__["MUTER"].calls, [("set", True), ("set", False)])

    def test_filler_lines_are_queued_while_muted(self):
        # 'Bear with me', 'Still at it.', acknowledgements, alerts: all go through Mouth.say and now play silently
        m = make_mouth()
        m.set_muted(True)
        for filler in ("Bear with me.", "Still at it.", "Online.", "Cancelled."):
            m.say(filler)
        self.assertEqual(m.q.qsize(), 4)

    def test_markers_still_never_spoken_while_muted(self):
        m = make_mouth()
        m.set_muted(True)
        m.say("[quiet]")
        m.say("**[done]**")
        self.assertTrue(m.q.empty())

    def test_speaking_stays_true_while_muted_so_echo_gate_keeps_the_mic_guarded(self):
        # the echo gate asks mouth.speaking; muted speech is still 'speaking' (the room may still hear it, and the
        # head, the guard and the queue all run on real time)
        m = make_mouth()
        m.say("a long reply")
        m.say("another")
        m.set_muted(True)
        self.assertTrue(m.speaking)
        m.q.get(); m.q.get()
        m.busy = True
        self.assertTrue(m.speaking, "mid-clip while muted")

    def test_stop_while_muted_still_really_stops(self):
        m = make_mouth()
        m.set_muted(True)
        m.say("a")
        m.say("b")
        m.stop()
        self.assertTrue(m.stop_evt.is_set())
        self.assertTrue(m.q.empty())
        self.assertTrue(m.muted, "stop does not unmute")

    def test_backlog_logged_once_after_two_minutes_muted(self):
        m = make_mouth()
        m.set_muted(True)
        m.muted_since = time.time() - audio_mute.BACKLOG_S - 1
        for i in range(6):
            m.say(f"reply {i}")
        lines = [t for w, t in MOUTH_LOG if "Still muted" in t]
        self.assertEqual(len(lines), 1, "exactly one line, not one per reply")
        self.assertEqual(m.q.qsize(), 6, "and nothing dropped")

    def test_no_backlog_line_for_a_short_mute_or_small_queue(self):
        m = make_mouth()
        m.set_muted(True)
        for i in range(6):
            m.say(f"reply {i}")
        self.assertEqual(MOUTH_LOG, [], "muted for seconds only")
        m2 = make_mouth()
        m2.set_muted(True)
        m2.muted_since = time.time() - 500
        m2.say("one")
        m2.say("two")
        self.assertEqual(MOUTH_LOG, [], "queue under 3")

    def test_backlog_flag_resets_on_the_next_mute(self):
        m = make_mouth()
        m.set_muted(True)
        m.backlog_logged = True
        m.set_muted(False)
        m.set_muted(True)
        self.assertFalse(m.backlog_logged)
    def test_mute_when_idle_is_harmless_and_double_mute_ok(self):
        m = make_mouth()
        m.set_muted(True)
        m.set_muted(True)
        self.assertTrue(m.muted)
        m.set_muted(False)
        m.set_muted(False)
        self.assertFalse(m.muted)

    def test_starts_unmuted_and_not_persisted_in_source(self):
        # Mouth.__init__ sets muted False; nothing in the mute path writes state.json
        self.assertRegex(VOICE_SRC, r"self\.muted = False\s+# global override")
        self.assertNotIn("state[\"muted\"]", VOICE_SRC)
        self.assertNotIn("state['muted']", VOICE_SRC)
        self.assertNotIn("muted", "".join(re.findall(r"STATE_KEYS[^\n]*", VOICE_SRC)))

    def test_stop_and_shut_up_still_only_stop_speech_not_mute(self):
        m = make_mouth()
        m.say("hello there")
        m.stop()
        self.assertFalse(m.muted)
        self.assertTrue(m.q.empty())
        m.say("still works afterwards")
        self.assertFalse(m.q.empty())


# ------------------------------------------------------------------ App.set_muted + voice commands
class FakeGate:
    def __init__(self):
        self.barged = []

    def barge_in(self, hold_s=0.8):
        self.barged.append(hold_s)


def make_app():
    logs, saves = [], []

    def no_save(*a, **k):
        saves.append(a)
        raise AssertionError("Mute must never be saved")

    App = lift(VOICE_TREE, "App", ["set_muted", "_local_command"],
               {"log": lambda who, text: logs.append((who, text)), "save_state": no_save,
                "windowless": None, "mute": mute})
    App.muted = property(lambda self: bool(self.mouth.muted))        # the real one is a @property too
    app = App.__new__(App)
    app.mouth = make_mouth()
    spoken = []
    real_say = app.mouth.say
    app.mouth.say = lambda t: (spoken.append(t), real_say(t))[0]
    app.ears = types.SimpleNamespace(gate=FakeGate())
    app.state = {}
    app.stopped = []
    real_stop = app.mouth.stop
    app.mouth.stop = lambda: (app.stopped.append(1), real_stop())[0]
    app.on_cancel = lambda: None
    app._quit_gate_check = lambda t: False
    return app, logs, spoken


class TestAppMute(unittest.TestCase):
    def test_set_muted_keeps_the_mic_gate_guarded_and_logs(self):
        app, logs, _ = make_app()
        app.set_muted(True)
        self.assertTrue(app.muted)
        self.assertEqual(app.ears.gate.barged, [], "echo guard is NOT released: AIDA is still speaking, silently")
        self.assertEqual(app.stopped, [], "muting never stops the speech")
        self.assertTrue(any("Muted" in t for _, t in logs))

    def test_set_muted_same_state_is_a_noop(self):
        app, logs, _ = make_app()
        app.set_muted(False)
        self.assertEqual(logs, [])
        self.assertEqual(app.ears.gate.barged, [])

    def test_voice_mute_is_logged_not_spoken(self):
        app, logs, spoken = make_app()
        self.assertTrue(app._local_command("Mute."))
        self.assertTrue(app.muted)
        self.assertEqual(spoken, [])

    def test_voice_unmute_is_log_only_and_does_not_speak(self):
        # unmuting now continues the current speech, so an extra 'Unmuted.' line would be noise
        app, logs, spoken = make_app()
        app._local_command("mute")
        self.assertTrue(app._local_command("unmute"))
        self.assertFalse(app.muted)
        self.assertEqual(spoken, [])
        self.assertTrue(app.mouth.q.empty())
        self.assertTrue(any("Unmuted" in t for _, t in logs))

    def test_unmute_yourself_and_mute_yourself(self):
        app, _, spoken = make_app()
        app._local_command("mute yourself")
        self.assertTrue(app.muted)
        app._local_command("Unmute yourself!")
        self.assertFalse(app.muted)

    def test_stop_and_shut_up_unchanged(self):
        # they cut current speech only: mouth.stop() is called, mute state untouched
        for phrase in ("stop", "shut up", "Okay, stop talking", "be quiet", "quiet", "shush"):
            app, _, _ = make_app()
            self.assertTrue(app._local_command(phrase), phrase)
            self.assertEqual(app.stopped, [1], phrase)
            self.assertFalse(app.muted, phrase)

    def test_unmute_when_not_muted_is_harmless(self):
        app, _, spoken = make_app()
        self.assertTrue(app._local_command("unmute"))
        self.assertFalse(app.muted)

    def test_voice_turn_is_also_carried_on_silently_while_muted(self):
        # voice replies go through the same Mouth.say: queued and played (silently) so unmute picks up in place
        app, _, _ = make_app()
        app.set_muted(True)
        app.mouth.say("a reply to a spoken question")
        self.assertEqual(app.mouth.q.qsize(), 1)

    def test_mute_state_never_saved(self):
        app, _, _ = make_app()
        app.set_muted(True)          # make_app's save_state raises if called
        app.set_muted(False)
        self.assertEqual(app.state, {})


class TestParse(unittest.TestCase):
    def test_mute_forms(self):
        for t in ("mute", "Mute.", "mute yourself", "okay mute", "please mute", "mute your voice", "Mute yourself, please"):
            self.assertIs(mute.parse_command(t), True, t)

    def test_unmute_forms(self):
        for t in ("unmute", "Unmute!", "unmute yourself", "ok unmute", "unmute your voice"):
            self.assertIs(mute.parse_command(t), False, t)

    def test_not_mute_commands(self):
        # stop / shut up / quiet are the existing 'cut current speech' commands, not mute. Invalid and odd inputs too.
        for t in ("stop", "shut up", "be quiet", "quiet", "", None, "mutex", "mute the music player",
                  "can you mute my speakers", "unmuted", "head off", "this is a mute point"):
            self.assertIsNone(mute.parse_command(t), repr(t))


# ------------------------------------------------------------------ switch widget, tooltip, badge
class FakeSwitch:
    def __init__(self, on=False):
        self.on = on

    def get(self):
        return self.on

    def set(self, on, animate=True):
        self.on = bool(on)


class FakeBadge:
    def __init__(self):
        self.shown = False

    def pack(self, **kw):
        self.shown = True

    def pack_forget(self):
        self.shown = False


def make_window(app):
    Win = lift(CHAT_TREE, "ChatWindow", ["toggle_mute", "_paint_mute"])
    w = Win.__new__(Win)
    w.app, w.s = app, 1.0
    w.state = {}
    w.mute_lbl, w.mute_badge, w._badge_shown = FakeSwitch(), FakeBadge(), False
    return w


class FakeCanvas:
    def __init__(self):
        self.fills = []

    def delete(self, *a):
        self.fills.clear()

    def create_oval(self, *a, fill=None, **k):
        self.fills.append(fill)

    def create_rectangle(self, *a, fill=None, **k):
        self.fills.append(fill)


class TestWindow(unittest.TestCase):
    def test_switch_model_has_warning_colour_distinct_from_green(self):
        self.assertEqual(cw.MUTE_ON, "#D9A93A")
        self.assertNotEqual(cw.MUTE_ON.lower(), cw.SWITCH_ON.lower())
        self.assertEqual(cw.MUTE_ON, cw.PALETTE["system"][0], "colour comes from PALETTE")

    def test_switch_draws_amber_track_when_muted_and_green_for_others(self):
        def draw(model, t):
            sw = cw.ToggleSwitch.__new__(cw.ToggleSwitch)
            sw.m, sw.cv, sw.w, sw.h, sw.s, sw.t, sw.hover = model, FakeCanvas(), 36, 20, 1.0, t, False
            sw._draw()
            return sw.cv.fills[0]
        mute_model = cw.SwitchModel("Mute", "tip", True)
        mute_model.on_colour = cw.MUTE_ON
        self.assertEqual(draw(mute_model, 1.0).lower(), cw.MUTE_ON.lower())
        self.assertEqual(draw(cw.SwitchModel("Speak replies", "tip", True), 1.0).lower(), cw.SWITCH_ON.lower())
        self.assertEqual(draw(mute_model, 0.0).lower(), cw.SWITCH_OFF.lower(), "off looks the same as the other switches")

    def test_tooltip_text(self):
        want = "Silences the speaker. I keep talking, so unmuting picks up where I've got to."
        self.assertEqual(cw.MUTE_TIP, want)
        self.assertEqual(mute.MUTE_TIP, want, "mute.py and the window fallback agree")
        self.assertIn(want, CHAT_SRC, "the fallback inside chat_window.py has the same text")
        self.assertEqual(cw.MUTE_LABEL, "Mute")

    def test_existing_switch_tables_untouched(self):
        # Mute lives beside, not inside, the three original switch tables
        self.assertEqual(cw.SWITCH_LABELS, {"head": "Head", "speak": "Speak replies", "top": "Always on top"})

    def test_switch_is_built_between_speak_and_pin_in_the_row(self):
        m = re.search(r"for sw in \(([^)]*)\):", CHAT_SRC)
        self.assertEqual([x.strip() for x in m.group(1).split(",")],
                         ["self.head_lbl", "self.speak_lbl", "self.mute_lbl", "self.pin_lbl"])

    def test_click_toggles_app_and_paints_badge(self):
        app, _, _ = make_app()
        w = make_window(app)
        w.toggle_mute()
        self.assertTrue(app.muted)
        self.assertTrue(w.mute_lbl.get())
        self.assertTrue(w.mute_badge.shown, "header shows the Muted badge")
        w.toggle_mute()
        self.assertFalse(app.muted)
        self.assertFalse(w.mute_lbl.get())
        self.assertFalse(w.mute_badge.shown)

    def test_voice_command_is_mirrored_by_the_poll(self):
        # 'mute' said aloud flips the app; the window's tick then syncs switch and badge
        app, _, _ = make_app()
        w = make_window(app)
        app._local_command("mute")
        w._paint_mute()
        self.assertTrue(w.mute_lbl.get() and w.mute_badge.shown)
        app._local_command("unmute")
        w._paint_mute()
        self.assertFalse(w.mute_lbl.get() or w.mute_badge.shown)

    def test_independent_of_speak_replies_all_four_combinations(self):
        # Speak replies is Dispatcher/ChatWindow state sent as speak=...; muting never touches it, and
        # whatever the combination, a reply is queued exactly when Speak replies is on (mute no longer drops it).
        for speak in (True, False):
            for muted in (True, False):
                app, _, _ = make_app()
                w = make_window(app)
                w.speak = speak
                app.set_muted(muted)
                w._paint_mute()
                self.assertEqual(w.speak, speak, "mute must not change Speak replies")
                self.assertNotIn("chat_speak", w.state)
                # the app only calls mouth.say for a reply when speak is true; model that gate
                if speak:
                    app.mouth.say("reply")
                self.assertEqual(not app.mouth.q.empty(), speak, f"speak={speak} muted={muted}")

    def test_window_never_persists_mute(self):
        body = ast.get_source_segment(CHAT_SRC, next(
            n for c in CHAT_TREE.body if isinstance(c, ast.ClassDef) and c.name == "ChatWindow"
            for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "toggle_mute"))
        self.assertNotIn("save_state", body)
        self.assertNotIn("self.state", body)

    def test_window_tolerates_an_app_without_mute_support(self):
        w = make_window(types.SimpleNamespace())
        w.toggle_mute()                       # no set_muted: must not raise
        w._paint_mute()
        self.assertFalse(w.mute_badge.shown)


class TestScope(unittest.TestCase):
    def test_mute_not_in_settings_menu_and_no_hotkey(self):
        self.assertNotIn("mute", (HERE / "hotkey_ui.py").read_text(encoding="utf-8-sig").lower())
        self.assertNotIn("mute", (HERE / "hotkeys.py").read_text(encoding="utf-8-sig").lower())

    def test_beeps_and_chime_do_not_go_through_mouth(self):
        # beep()/chime() hand WAV tones straight to SOUNDS (sounds.py), never to Mouth. They share the process audio
        # session, so session-mute silences them too while the switch is on (decided and documented: no special case)
        for name in ("beep", "chime"):
            src = ast.get_source_segment(VOICE_SRC, next(n for n in VOICE_TREE.body
                                                         if isinstance(n, ast.FunctionDef) and n.name == name))
            self.assertIn("SOUNDS.play", src)
            self.assertNotIn("mouth", src.lower())
            self.assertNotIn("muted", src.lower())

    def test_every_spoken_path_goes_through_mouth_say(self):
        # no second speech route that could bypass the mute: SAPI/Kokoro are only used inside Mouth
        self.assertEqual(len(re.findall(r"SAPI\.SpVoice", VOICE_SRC)), 1)
        self.assertEqual(len(re.findall(r"\.kokoro\.create|k\.create\(", VOICE_SRC)) >= 1, True)


# ------------------------------------------------------------------ the session muter (fake Core Audio, no real audio)
class TestCarryOn(unittest.TestCase):
    def test_timeline_mute_at_10_unmute_at_20_resumes_at_20_not_10_not_0(self):
        # a 30 s clip. The playhead is wall-clock and never touched by Mute; only audibility changes.
        muter, be = make_muter()
        vol = be.vols[0]
        audible = []
        for tenth in range(0, 301):
            t = tenth / 10
            if tenth == 100:
                muter.set(True)
            if tenth == 200:
                muter.set(False)
            audible.append((t, not vol.muted))
        self.assertTrue(all(a for t, a in audible if t < 10), "audible before the mute")
        self.assertFalse(any(a for t, a in audible if 10 <= t < 20), "silent for the whole muted window")
        self.assertTrue(all(a for t, a in audible if t >= 20), "audible again from 20 s")
        first_back = min(t for t, a in audible if t >= 10 and a)
        self.assertEqual(first_back, 20.0, "resumes at the CURRENT playhead (20 s), not 10 s and not 0")
        self.assertEqual(audible[-1][0], 30.0, "the clip still ends on schedule (30 s, not 40 s)")

    def test_mute_never_touches_playback_only_the_session(self):
        muter, be = make_muter()
        muter.set(True)
        self.assertEqual({c[0] for c in be.vols[0].calls}, {"mute", "vol"}, "only mute/volume calls exist")

    def test_unmute_after_the_speech_ended_is_harmless(self):
        muter, be = make_muter([])            # no session: nothing playing, nothing to find
        muter.set(True)
        muter.set(False)
        self.assertEqual(muter.last_count, 0)
        self.assertEqual(muter.errors, 0)

    def test_mute_with_nothing_playing_is_state_only_then_applied_when_a_clip_starts(self):
        muter, be = make_muter([])
        muter.set(True)
        self.assertTrue(muter.desired)
        self.assertFalse(muter.effective, "honest: muted but no session found yet")
        v = FakeVol()
        be.vols.append(v)                     # the next clip opens its session
        muter.reapply()
        self.assertTrue(v.muted, "mute flag is re-applied when a new playback starts")
        self.assertTrue(muter.effective)

    def test_reapply_clears_a_stale_persisted_mute_when_unmuted(self):
        # Windows remembers per-app mute across runs (proved live). A fresh session that arrives muted gets cleared.
        stale = FakeVol(muted=True)
        muter, _ = make_muter([stale])
        muter.reapply()
        self.assertFalse(stale.muted)
        self.assertEqual(stale.volume, 1.0)

    def test_restore_unmutes_everything_and_resets_desired(self):
        a, b = FakeVol(muted=True), FakeVol(muted=True, volume=0.3)
        muter, _ = make_muter([a, b])
        muter.desired = True
        muter.restore()
        self.assertFalse(muter.desired)
        self.assertEqual((a.muted, b.muted, b.volume), (False, False, 1.0), "a crashed half-ramp is repaired too")

    def test_shutdown_leaves_session_unmuted_on_the_exit_path(self):
        muter, be = make_muter()
        muter.set(True)
        self.assertTrue(be.vols[0].muted)
        muter.shutdown()
        self.assertFalse(be.vols[0].muted)

    def test_startup_restores_unmuted_and_exit_hook_is_registered(self):
        src = VOICE_SRC
        self.assertRegex(src, r"MUTER = audio_mute\.SessionMuter\(")
        self.assertIn("audio_mute.install_exit_restore(MUTER)", src)
        started = audio_mute.SessionMuter(backend=FakeBackend([FakeVol(muted=True)]), sleep=lambda s: None)
        try:
            started.restore(wait=True)
            self.assertFalse(started.backend.vols[0].muted, "worker thread un-mutes on start-up")
        finally:
            started.shutdown()

    def test_mute_state_is_not_persisted_by_the_muter(self):
        src = (HERE / "audio_mute.py").read_text(encoding="utf-8")
        for bad in ("json", "open(", "write_text", "state.json"):
            self.assertNotIn(bad, src.replace("# ", ""), bad)

    def test_idempotent_toggles_do_not_repeat_work(self):
        muter, be = make_muter()
        muter.set(True)
        n = len(be.vols[0].calls)
        muter.set(True)
        muter.reapply()
        self.assertEqual(len(be.vols[0].calls), n, "already muted: nothing more to do")
        muter.set(False)
        m = len(be.vols[0].calls)
        muter.set(False)
        self.assertEqual(len(be.vols[0].calls), m)

    def test_rapid_toggling_ends_in_the_last_requested_state(self):
        muter, be = make_muter()
        for i in range(101):
            muter.set(i % 2 == 0)             # ends on i=100 -> muted
        self.assertTrue(be.vols[0].muted)
        muter.set(False)
        self.assertFalse(be.vols[0].muted)
        self.assertEqual(be.vols[0].volume, 1.0, "volume is left at full, never stuck mid-fade")

    def test_rapid_toggling_through_the_worker_thread(self):
        v = FakeVol()
        muter = audio_mute.SessionMuter(backend=FakeBackend([v]), sleep=lambda s: None, poll_s=0.01)
        try:
            for i in range(60):
                muter.set(i % 2 == 0)
            muter.set(True, wait=True)
            self.assertTrue(v.muted)
            muter.set(False, wait=True)
            self.assertFalse(v.muted)
        finally:
            muter.shutdown()

    def test_worker_picks_up_a_new_session_while_muted(self):
        be = FakeBackend([])
        muter = audio_mute.SessionMuter(backend=be, sleep=lambda s: None, poll_s=0.01)
        try:
            muter.set(True, wait=True)
            late = FakeVol()
            be.vols.append(late)              # a clip starts later; nobody calls reapply
            for _ in range(100):
                if late.muted:
                    break
                time.sleep(0.01)
            self.assertTrue(late.muted, "the poll catches it within a few ms")
        finally:
            muter.shutdown()

    def test_all_sessions_of_the_process_are_matched(self):
        vols = [FakeVol(), FakeVol(), FakeVol()]
        muter, _ = make_muter(vols)
        muter.set(True)
        self.assertTrue(all(v.muted for v in vols))

    def test_device_change_new_session_object_is_matched(self):
        muter, be = make_muter()
        muter.set(True)
        be.vols[:] = [FakeVol()]              # default device switched: brand-new session objects
        muter.reapply()
        self.assertTrue(be.vols[0].muted)

    def test_device_disconnect_errors_are_swallowed_and_logged_once(self):
        logs = []
        be = FakeBackend(fail=OSError("device invalidated"))
        muter = audio_mute.SessionMuter(backend=be, start=False, log=lambda w, t: logs.append(t))
        for _ in range(5):
            muter.set(True)
        self.assertEqual(len(logs), 1, "one line, not one per attempt")
        self.assertEqual(muter.errors, 5)
        self.assertFalse(muter.effective)
        be.fail = None
        be.vols.append(FakeVol())
        muter.reapply()
        self.assertTrue(be.vols[0].muted, "recovers when the device comes back")

    def test_a_failing_session_does_not_block_the_others(self):
        class Bad(FakeVol):
            def set_mute(self, f):
                raise OSError("exclusive mode")
        good = FakeVol()
        muter, _ = make_muter([Bad(), good])
        muter.set(True)
        self.assertTrue(good.muted)

    def test_init_failure_does_not_kill_the_worker(self):
        class BadInit(FakeBackend):
            def init_thread(self):
                raise OSError("no COM")
        v = FakeVol()
        muter = audio_mute.SessionMuter(backend=BadInit([v]), sleep=lambda s: None)
        try:
            muter.set(True, wait=True)
            self.assertTrue(v.muted)
        finally:
            muter.shutdown()


class TestRamp(unittest.TestCase):
    def test_ramp_levels_monotonic_and_strictly_inside_0_1(self):
        down, up = audio_mute.ramp_levels(down=True), audio_mute.ramp_levels(down=False)
        self.assertEqual(down, sorted(down, reverse=True))
        self.assertEqual(up, sorted(up))
        self.assertTrue(all(0 < g < 1 for g in down + up))
        self.assertEqual(len(down), audio_mute.RAMP_STEPS - 1)
        self.assertEqual(down, list(reversed(up)))

    def test_ramp_is_short_enough_to_feel_instant(self):
        self.assertLessEqual(audio_mute.RAMP_MS, 20)
        self.assertGreaterEqual(audio_mute.RAMP_MS, 5)

    def test_mute_fades_down_then_mutes_then_restores_volume(self):
        muter, be = make_muter()
        muter.set(True)
        calls = be.vols[0].calls
        mute_at = calls.index(("mute", True))
        fades = [v for k, v in calls[:mute_at] if k == "vol"]
        self.assertEqual(fades, [round(g, 3) for g in audio_mute.ramp_levels(down=True)])
        self.assertEqual(calls[mute_at + 1], ("vol", 1.0), "volume back to full while silent")

    def test_unmute_starts_silent_then_fades_up_to_full(self):
        muter, be = make_muter()
        muter.set(True)
        be.vols[0].calls.clear()
        muter.set(False)
        calls = be.vols[0].calls
        self.assertEqual(calls[0], ("vol", 0.0))
        self.assertEqual(calls[1], ("mute", False))
        self.assertEqual([v for k, v in calls[2:] if k == "vol"][-1], 1.0)

    def test_ramp_can_be_switched_off(self):
        muter, be = make_muter(ramp=False)
        muter.set(True)
        self.assertEqual(be.vols[0].calls, [("mute", True)])


class TestRouting(unittest.TestCase):
    def test_playback_starts_re_apply_the_mute_flag(self):
        # each way of starting a sound must call MUTER.reapply: Kokoro stream, AIDA-filter clip, SAPI, beeps
        self.assertRegex(VOICE_SRC, r"callback=feed\):\s*\n\s*MUTER\.reapply\(wait=True\)")
        self.assertRegex(VOICE_SRC, r"SND_ASYNC\)\s*\n\s*MUTER\.reapply\(\)")
        self.assertRegex(VOICE_SRC, r"MUTER\.reapply\(\)\s*\n\s*v\.Speak\(text, 1\)")
        self.assertRegex(VOICE_SRC, r"SOUNDS\.play\(kind\)\s*\n\s*MUTER\.reapply\(\)")

    def test_stop_cancel_and_barge_in_still_really_stop(self):
        stop_src = ast.get_source_segment(VOICE_SRC, next(
            f for c in VOICE_TREE.body if isinstance(c, ast.ClassDef) and c.name == "Mouth"
            for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "stop"))
        self.assertIn("stop_evt.set()", stop_src)
        self.assertIn("get_nowait", stop_src, "queue is flushed by stop")
        self.assertRegex(VOICE_SRC, r"talking over it shuts it up")       # talk hotkey -> mouth.stop()

    def test_mute_does_not_set_the_stop_event_or_drop_in_the_run_loop(self):
        mouth = next(c for c in VOICE_TREE.body if isinstance(c, ast.ClassDef) and c.name == "Mouth")
        for name in ("set_muted", "say"):
            body = ast.get_source_segment(VOICE_SRC, next(f for f in mouth.body
                                                          if isinstance(f, ast.FunctionDef) and f.name == name))
            self.assertNotIn("stop_evt", body)
            self.assertNotIn("self.stop()", body)
        run = ast.get_source_segment(VOICE_SRC, next(f for f in mouth.body
                                                     if isinstance(f, ast.FunctionDef) and f.name == "run"))
        self.assertNotIn("self.muted", run, "the audio thread no longer skips muted lines")

    def test_core_audio_interfaces_declared_with_correct_iids(self):
        src = (HERE / "audio_mute.py").read_text(encoding="utf-8")
        for iid in ("87CE5498-68D6-44E5-9215-6DA47EF883D8",      # ISimpleAudioVolume
                    "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F",      # IAudioSessionManager2
                    "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D",      # IAudioSessionControl2
                    "A95664D2-9614-4F35-A746-DE8DB63617E6",      # IMMDeviceEnumerator
                    "BCDE0395-E52F-467C-8E3D-C4579291692E"):     # MMDeviceEnumerator CLSID
            self.assertIn(iid, src)

    def test_no_new_dependency_pycaw(self):
        reqs = (HERE / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
        self.assertNotIn("pycaw", reqs)

    def test_real_backend_constructs_without_touching_audio(self):
        be = audio_mute.RealSessions(pid=1)
        self.assertEqual(be.core.pid, 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
