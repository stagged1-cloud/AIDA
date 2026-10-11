"""Tests for screen_record.py (parser, monitor labelling, controller state machine) with a fake recorder.
No OBS, no Tk, no real recording. Each test says what it proves."""
import threading
import time
import unittest

import screen_record as sr

LEFT = {"index": 0, "name": "DISPLAY1", "rect": [0, 0, 1920, 1080]}
MID = {"index": 1, "name": "DISPLAY2", "rect": [1920, 0, 3840, 1080]}
RIGHT = {"index": 2, "name": "DISPLAY3", "rect": [3840, 0, 5760, 1080]}


class FakeRecorder:
    """Same interface as obs_control.ObsRecorder; every call returns a dict and records itself in `calls`."""

    def __init__(self, monitors=(LEFT,), ready=None, start=None, stop=None, delay=0.0):
        self.monitors, self.delay, self.calls = list(monitors), delay, []
        self.ready = ready or {"ok": True, "error": ""}
        self._start = start or {"ok": True, "path": "C:/Videos/2026-10-10 20-30-00.mkv", "error": ""}
        self._stop = stop or {"ok": True, "path": "C:/Videos/2026-10-10 20-30-00.mkv", "error": ""}
        self.gate = None                      # optional threading.Event the start call waits on

    def ensure_ready(self):
        self.calls.append("ensure_ready")
        return dict(self.ready)

    def list_monitors(self):
        self.calls.append("list_monitors")
        return {"ok": True, "error": "", "monitors": self.monitors}

    def start(self, monitor_index=None, monitor_indexes=None, **kw):
        self.start_kw = kw                    # audio_desktop / audio_mic / record_dir / mic_device / remember, if passed
        self.calls.append(("start", monitor_indexes if monitor_indexes is not None else monitor_index))
        if self.gate:
            self.gate.wait(5)
        time.sleep(self.delay)
        return dict(self._start)

    def stop(self):
        self.calls.append("stop")
        return dict(self._stop)


class MultiAndStop(unittest.TestCase):
    """Added with the OBS start/stop fix: multi-monitor start, honest state, graceful stop, number/text fields."""

    def test_start_monitors_two_goes_to_recorder_as_list_and_names_both(self):
        # Picker ticked both screens: one recorder call with the list, state 'recording', logged 'both screens'.
        c, spoken, notes, _ = make(monitors=(LEFT, MID))
        self.assertEqual(c.start(monitors=[1, 0]), "starting")
        c.join()
        self.assertEqual(c.state, "recording")
        self.assertIn(("start", [0, 1]), c.rec.calls)          # sorted by index when no listing is available
        self.assertEqual(c.screen_name, "both screens")
        self.assertIn(("info", "Recording both screens."), notes)

    def test_start_monitors_single_uses_plain_index(self):
        c, *_ = make(monitors=(LEFT, MID))
        c.start(monitors=[1])
        c.join()
        self.assertIn(("start", 1), c.rec.calls)
        self.assertNotIn("list_monitors", c.rec.calls)

    def test_recorder_failure_means_never_recording(self):
        # OBS did not confirm: state idle, error logged with the real reason, nothing says 'Recording'.
        c, spoken, notes, _ = make(start={"ok": False, "error": "OBS did not start recording within 5 seconds."})
        c.start(monitors=[0])
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertFalse(any("Recording" in t for k, t in notes if k == "info"))
        self.assertTrue(any(k == "error" for k, t in notes))

    def test_controlled_folder_reason_passes_through_plainly(self):
        msg = "OBS was blocked from writing to C:/Users/x/Videos (Unable to open file). Controlled Folder Access."
        self.assertTrue(sr.map_error(msg, "start").startswith("OBS was blocked from writing"))

    def test_stop_when_obs_not_recording_goes_idle_with_message(self):
        # The screenshot case: UI believed recording, OBS says no. Back to idle, 'Not recording.', no error.
        c, spoken, notes, _ = make(stop={"ok": False, "error": "OBS is not recording", "path": ""})
        c.start(monitors=[0])
        c.join()
        c.stop()
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertIn(("info", "Not recording."), notes)
        self.assertFalse(any(k == "error" for k, t in notes))

    def test_stop_returns_all_paths(self):
        c, *_ = make(stop={"ok": True, "error": "", "path": "C:/V/a.mp4", "paths": ["C:/V/a.mp4", "C:/V/b.mp4"]})
        c.start(monitors=[0])
        c.join()
        c.stop()
        c.join()
        self.assertEqual(c.last_paths, ["C:/V/a.mp4", "C:/V/b.mp4"])
        self.assertEqual(c.last_path, "C:/V/a.mp4")

    def test_label_monitors_has_number_and_text(self):
        lab = sr.label_monitors([RIGHT, LEFT, MID])
        self.assertEqual([m["number"] for m in lab], [1, 2, 3])
        self.assertEqual(lab[0]["text"], "Screen 1 (left) 1920x1080")
        self.assertEqual(lab[2]["text"], "Screen 3 (right) 1920x1080")


def make(rec=None, **kw):
    spoken, notes, asked = [], [], []
    c = sr.ScreenRecordController(rec or FakeRecorder(**kw), speak=spoken.append,
                                  notify=lambda k, t: notes.append((k, t)))
    c.choose = lambda labelled, source: asked.append((labelled, source))
    return c, spoken, notes, asked


class ParserPositives(unittest.TestCase):
    def check(self, text, want):
        self.assertEqual(sr.parse_record_command(text), want, text)

    def test_start_phrases(self):
        # The plain ways the user would say it all start with no monitor chosen.
        for t in ["start recording", "Start recording.", "record the screen", "Record screen", "start the recording",
                  "begin recording", "start screen recording", "screen record", "OK, start recording please",
                  "can you start recording", "Right, record the screen now", "start recording the screen"]:
            self.check(t, ("start", None))

    def test_start_with_side(self):
        # left/middle/right in the various word orders; 'laptop' and 'centre' mean middle.
        self.check("record my left screen", ("start", "left"))
        self.check("Record the right hand screen", ("start", "right"))
        self.check("start recording the middle monitor", ("start", "middle"))
        self.check("record the laptop screen", ("start", "middle"))
        self.check("record the centre screen", ("start", "middle"))
        self.check("record the screen on the right", ("start", "right"))
        self.check("start screen recording on the left", ("start", "left"))

    def test_start_with_number(self):
        # Spoken numbers, digits, ordinals, and the 'to' transcription slop all give a 1-based ordinal.
        self.check("record screen two", ("start", 2))
        self.check("record screen 3", ("start", 3))
        self.check("start recording screen one", ("start", 1))
        self.check("record screen to", ("start", 2))

    def test_stop_phrases(self):
        for t in ["stop recording", "Stop the recording.", "stop screen recording", "end the recording",
                  "OK stop recording please", "stop recording the screen", "finish recording"]:
            self.check(t, ("stop", None))

    def test_status_phrases(self):
        for t in ["are you recording?", "Are you still recording", "is it recording", "is the screen recording",
                  "are we recording?", "is the recording still on"]:
            self.check(t, ("status", None))

    def test_punctuation_case_and_apostrophes(self):
        # Transcription gives odd case, commas and curly apostrophes.
        self.check("OK, AIDA, Start   Recording!", ("start", None))
        self.check("Let\u2019s start recording", ("start", None))


class ParserNegatives(unittest.TestCase):
    def test_ordinary_sentences_do_not_trigger(self):
        # 'record' appears but it is not a screen-recording request: must never be hijacked from Claude.
        for t in ["record the meeting notes in a file", "what's the world record", "start recording the meeting",
                  "I want to record a voice memo", "break the record", "record", "stop", "stop talking",
                  "keep a record of this", "the recording of the call is in Teams", "start the engine",
                  "please record this decision in decisions.md", "", None, "   ", "stop the music"]:
            self.assertEqual(sr.parse_record_command(t), (None, None), t)

    def test_bare_record_with_non_screen_object(self):
        self.assertEqual(sr.parse_record_command("record the left"), (None, None))   # not a screen phrase
        self.assertEqual(sr.parse_record_command("record my screen time"), (None, None))

    def test_answers(self):
        # Replies to 'which screen?'.
        a = sr.parse_monitor_answer
        self.assertEqual(a("left"), "left")
        self.assertEqual(a("the middle one"), "middle")
        self.assertEqual(a("Right."), "right")
        self.assertEqual(a("right one"), "right")          # 'right' as a filler word must not be stripped here
        self.assertEqual(a("screen two"), 2)
        self.assertEqual(a("three"), 3)
        self.assertIsNone(a("banana"))
        self.assertIsNone(a("what time is it"))
        self.assertIsNone(a(""))


class ParserEmbedded(unittest.TestCase):
    # Open-mic run-ons: the command sits in one clause among chatter (10 Oct 22:26, went to Claude instead).
    def test_don_run_on_goes_to_claude(self):
        # 22:43 rule: other content in the utterance -> Claude, not a local recording.
        t = ("okay, so, Hey AIDA did initiate the recording, so there's a good start, um, I want you to record "
             "the screen for me, screen one, thanks, um, what else,")
        self.assertEqual(sr.parse_record_command(t), (None, None))

    def test_multi_step_instruction_goes_to_claude(self):
        # The user, 10 Oct 22:43: duration + stop + open + play must reach Claude, not be swallowed as 'record screen 1'.
        t = ("um, ok, record screen 1 with sound, and um, what I want you to do after that is, actually, if you could "
             "record the screen for maybe 30 seconds and then stop the recording after 30 seconds, then I want you "
             "to open the recording on the right hand screen and play it, ok, send it")
        self.assertEqual(sr.parse_record_command_ex(t), (None, None, None))

    def test_duration_goes_to_claude(self):
        self.assertEqual(sr.parse_record_command("record screen one for 30 seconds"), (None, None))
        self.assertEqual(sr.parse_record_command("um, record screen one, for 30 seconds"), (None, None))

    def test_fillers_around_clean_command_still_local(self):
        self.assertEqual(sr.parse_record_command("um, ok, record screen one, send it"), ("start", 1))
        self.assertEqual(sr.parse_record_command_ex("um, record screen one with sound, and um, ok, send it")[0], "start")
        self.assertEqual(sr.parse_record_command("record the screen for me, screen one, thanks"), ("start", 1))

    def test_embedded_clause_with_monitor(self):
        self.assertEqual(sr.parse_record_command("right, um, record the left screen for me, cheers"), ("start", "left"))

    def test_embedded_stop(self):
        self.assertEqual(sr.parse_record_command("okay, that's enough, stop recording, thanks"), ("stop", None))

    def test_embedded_no_screen_named(self):
        self.assertEqual(sr.parse_record_command("um, start recording, what else"), ("start", None))

    def test_for_me_tail(self):
        self.assertEqual(sr.parse_record_command("record the screen for me"), ("start", None))

    def test_embedded_negatives(self):
        # Chatter that mentions recording must still go to Claude; status questions are never pulled out of a run-on.
        for t in ["okay, so, Hey AIDA did initiate the recording, so there's a good start",
                  "um, record the meeting notes in a file, thanks",
                  "so, are you recording, what else", "the recording of the call, is in Teams"]:
            self.assertEqual(sr.parse_record_command(t), (None, None), t)


class Monitors(unittest.TestCase):
    def test_three_screens_labelled_by_position_not_index(self):
        # Indices arrive in a shuffled order; labels follow the on-screen x position.
        lab = sr.label_monitors([RIGHT, LEFT, MID])
        self.assertEqual([m["label"] for m in lab], ["left", "middle", "right"])
        self.assertEqual([m["index"] for m in lab], [0, 1, 2])
        self.assertIn("1920x1080", lab[0]["desc"])

    def test_two_screens_are_left_right(self):
        self.assertEqual([m["label"] for m in sr.label_monitors([LEFT, MID])], ["left", "right"])

    def test_no_rects_falls_back_to_index_order_with_numbers(self):
        lab = sr.label_monitors([{"index": 1, "name": "B"}, {"index": 0, "name": "A"}])
        self.assertEqual([m["index"] for m in lab], [0, 1])
        self.assertTrue(lab[0]["desc"].startswith("Left screen") or lab[0]["desc"].startswith("Screen 1"))

    def test_resolve(self):
        lab = sr.label_monitors([LEFT, MID, RIGHT])
        self.assertEqual(sr.resolve_monitor("right", lab), 2)
        self.assertEqual(sr.resolve_monitor(2, lab), 1)
        self.assertIsNone(sr.resolve_monitor(4, lab))
        self.assertIsNone(sr.resolve_monitor(None, lab))
        self.assertIsNone(sr.resolve_monitor("middle", sr.label_monitors([LEFT, MID])))   # two screens: no 'middle'

    def test_question_lists_options(self):
        lab = sr.label_monitors([LEFT, MID, RIGHT])
        self.assertEqual(sr.choice_question(lab), "Which screen? left, middle or right?")

    def test_garbage_monitor_entries_ignored(self):
        self.assertEqual(sr.label_monitors([None, "x", {"name": "no index"}]), [])


class ErrorMapping(unittest.TestCase):
    def test_known_errors_become_actionable_sentences(self):
        cases = {"authentication failed": "password", "WebSocket connection refused": "WebSocket Server Settings",
                 "OBS is not running and could not be launched": "isn't running",
                 "OBS not installed": "can't find OBS", "No space left on device": "disk"}
        for raw, frag in cases.items():
            msg = sr.map_error(raw)
            self.assertIn(frag.lower(), msg.lower(), raw)
            self.assertTrue(msg.endswith("."))
            self.assertNotIn("\n", msg)

    def test_unknown_and_empty(self):
        self.assertIn("couldn't start", sr.map_error("kaboom happened. second sentence", "start"))
        self.assertNotIn("second sentence", sr.map_error("kaboom happened. second sentence", "start"))
        self.assertIn("no reason", sr.map_error("", "stop"))
        self.assertIn("no reason", sr.map_error(None))


class Controller(unittest.TestCase):
    def test_single_monitor_starts_straight_away(self):
        # Exactly one screen: no chooser, uses that monitor, announces by voice.
        c, spoken, notes, asked = make(monitors=[LEFT])
        self.assertEqual(c.start(source="voice"), "starting")
        c.join()
        self.assertEqual(c.state, "recording")
        self.assertEqual(asked, [])
        self.assertIn(("start", 0), c.rec.calls)
        self.assertEqual(spoken, ["Recording the screen."])

    def test_multiple_monitors_none_given_asks_instead_of_guessing(self):
        c, spoken, notes, asked = make(monitors=[LEFT, MID, RIGHT])
        c.start(source="ui")
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0][1], "ui")
        self.assertEqual([m["label"] for m in asked[0][0]], ["left", "middle", "right"])
        self.assertFalse(any(isinstance(x, tuple) and x[0] == "start" for x in c.rec.calls))   # never started

    def test_named_monitor_resolves_to_obs_index(self):
        c, spoken, notes, asked = make(monitors=[LEFT, MID, RIGHT])
        c.start("right", source="voice")
        c.join()
        self.assertIn(("start", 2), c.rec.calls)
        self.assertEqual(spoken, ["Recording the right screen."])
        self.assertEqual(c.state, "recording")

    def test_ordinal_monitor(self):
        c, *_ = make(monitors=[LEFT, MID, RIGHT])
        c.start(2)
        c.join()
        self.assertIn(("start", 1), c.rec.calls)

    def test_unmatched_monitor_falls_back_to_chooser(self):
        # 'middle' with only two screens can't be honoured: say so and ask, don't pick one.
        c, spoken, notes, asked = make(monitors=[LEFT, MID])
        c.start("middle", source="voice")
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertEqual(len(asked), 1)

    def test_chooser_pick_by_index_skips_listing(self):
        c, *_ = make(monitors=[LEFT, MID, RIGHT])
        c.start(index=1)
        c.join()
        self.assertEqual(c.state, "recording")
        self.assertNotIn("list_monitors", c.rec.calls)

    def test_no_chooser_callback_still_tells_the_user(self):
        c, spoken, notes, asked = make(monitors=[LEFT, MID])
        c.choose = None
        c.start()
        c.join()
        self.assertTrue(any("Which screen" in t for _, t in notes))

    def test_ensure_ready_runs_first_and_failure_stops_everything(self):
        c, spoken, notes, asked = make(ready={"ok": False, "error": "WebSocket connection refused"})
        c.start(source="voice")
        c.join()
        self.assertEqual(c.rec.calls, ["ensure_ready"])
        self.assertEqual(c.state, "idle")
        self.assertEqual(notes[0][0], "error")
        self.assertIn("WebSocket", notes[0][1])
        self.assertEqual(spoken, [notes[0][1]])             # spoken only for voice-initiated actions

    def test_ui_errors_are_not_spoken(self):
        c, spoken, notes, asked = make(ready={"ok": False, "error": "auth"})
        c.start(source="ui")
        c.join()
        self.assertEqual(spoken, [])
        self.assertEqual(notes[0][0], "error")
        self.assertTrue(c.last_error)

    def test_start_failure_returns_to_idle(self):
        c, spoken, notes, asked = make(start={"ok": False, "error": "disk full"})
        c.start()
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertIsNone(c.started_at)
        self.assertEqual(notes[0][0], "error")

    def test_recorder_raising_is_survived(self):
        class Boom(FakeRecorder):
            def ensure_ready(self):
                raise RuntimeError("explode")
        c, spoken, notes, asked = make(rec=Boom())
        c.start()
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertEqual(notes[0][0], "error")

    def test_stop_saves_path_and_speaks_short_path(self):
        c, spoken, notes, asked = make()
        c.start()
        c.join()
        self.assertEqual(c.stop(source="voice"), "stopping")
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertEqual(c.last_path, "C:/Videos/2026-10-10 20-30-00.mkv")
        self.assertEqual(spoken[-1], "Stopped, saved to Videos/2026-10-10 20-30-00.mkv.")

    def test_stop_failure_keeps_recording_state(self):
        c, spoken, notes, asked = make(stop={"ok": False, "error": "WebSocket connection refused"})
        c.start()
        c.join()
        c.stop()
        c.join()
        self.assertEqual(c.state, "recording")             # we don't know it stopped, so don't claim it did
        self.assertEqual(notes[-1][0], "error")

    def test_stop_when_idle(self):
        c, *_ = make()
        self.assertEqual(c.stop(), "not_recording")
        self.assertEqual(c.rec.calls, [])

    def test_start_when_recording_is_refused(self):
        c, *_ = make()
        c.start()
        c.join()
        self.assertEqual(c.start(), "already")

    def test_toggle(self):
        c, *_ = make()
        self.assertEqual(c.toggle(), "starting")
        c.join()
        self.assertEqual(c.toggle(), "stopping")
        c.join()
        self.assertEqual(c.state, "idle")

    def test_state_sequence_and_elapsed(self):
        c, *_ = make()
        self.assertEqual((c.state, c.elapsed()), ("idle", 0))
        c.start()
        c.join()
        self.assertEqual(c.state, "recording")
        c.started_at -= 125
        self.assertEqual(c.elapsed(), 125)
        self.assertIn("2 minutes 5 seconds", c.status_text())
        c.stop()
        c.join()
        self.assertEqual(c.elapsed(), 0)
        self.assertEqual(c.status_text(), "No, I'm not recording.")

    def test_start_does_not_block_the_caller(self):
        # The recorder blocks on a gate; start() must return at once, state is 'starting', and a second start is 'busy'.
        rec = FakeRecorder()
        rec.gate = threading.Event()
        c, *_ = make(rec=rec)
        t0 = time.time()
        self.assertEqual(c.start(), "starting")
        self.assertLess(time.time() - t0, 0.5)
        self.assertEqual(c.state, "starting")
        self.assertEqual(c.start(), "busy")
        self.assertEqual(c.stop(), "busy")
        rec.gate.set()
        c.join()
        self.assertEqual(c.state, "recording")

    def test_callbacks_that_raise_do_not_break_the_controller(self):
        c = sr.ScreenRecordController(FakeRecorder(), speak=lambda t: 1 / 0, notify=lambda k, t: 1 / 0)
        c.start(source="voice")
        c.join()
        self.assertEqual(c.state, "recording")


class DownRecorder(FakeRecorder):
    """OBS is 'down' for the first `down_for` ensure_ready calls, then answers. launch_obs only records the call."""

    def __init__(self, down_for=1, err="OBS is not running.", **kw):
        super().__init__(**kw)
        self.down_for, self.err, self.n = down_for, err, 0

    def ensure_ready(self):
        self.calls.append("ensure_ready")
        self.n += 1
        if self.n <= self.down_for:
            return {"ok": False, "error": self.err}
        return {"ok": True, "error": ""}

    def launch_obs(self):
        self.calls.append("launch_obs")
        return {"ok": True, "error": "", "launched": True}

    def status(self):
        self.calls.append("status")
        return {"ok": True, "obs_running": False, "recording": False}


class AutoLaunch(unittest.TestCase):
    def _mk(self, rec, **kw):
        c, spoken, notes, asked = make(rec)
        c.poll_interval, c.launch_timeout = 0.01, kw.get("timeout", 0.2)
        c.autolaunch = kw.get("autolaunch", True)
        return c, spoken, notes

    def test_obs_down_then_comes_up(self):
        # OBS not running -> launched once, progress spoken, then the normal start continues.
        rec = DownRecorder(down_for=3)
        c, spoken, notes = self._mk(rec)
        self.assertEqual(c.start(source="voice"), "starting")
        c.join()
        self.assertEqual(rec.calls.count("launch_obs"), 1)
        self.assertIn("Starting OBS, one moment.", spoken)
        self.assertIn(("start", 0), rec.calls)
        self.assertEqual(c.state, "recording")

    def test_obs_never_comes_up(self):
        # Never answers within the (shortened) timeout -> plain actionable error, back to idle, no start call.
        rec = DownRecorder(down_for=10 ** 6)
        c, spoken, notes = self._mk(rec, timeout=0.05)
        c.start(source="voice")
        c.join()
        self.assertEqual(c.state, "idle")
        self.assertNotIn(("start", 0), rec.calls)
        self.assertTrue(any(k == "error" for k, _ in notes))
        self.assertIn("OBS", c.last_error)

    def test_websocket_off_after_launch_gives_enable_hint(self):
        # OBS is up but websocket server is off -> message tells him where to enable it.
        rec = DownRecorder(down_for=10 ** 6, err="OBS is running but its WebSocket server is off: enable it in Tools > WebSocket Server Settings.")
        rec.n = 0
        first = [True]
        orig = rec.ensure_ready

        def er():
            if first[0]:
                first[0] = False
                rec.calls.append("ensure_ready")
                return {"ok": False, "error": "OBS is not running."}
            return orig()
        rec.ensure_ready = er
        c, spoken, notes = self._mk(rec, timeout=0.05)
        c.start()
        c.join()
        self.assertIn("WebSocket", c.last_error)

    def test_already_running_no_launch(self):
        # OBS already answers -> launch_obs never called.
        rec = DownRecorder(down_for=0)
        c, spoken, notes = self._mk(rec)
        c.start()
        c.join()
        self.assertNotIn("launch_obs", rec.calls)
        self.assertEqual(c.state, "recording")

    def test_status_query_does_not_launch(self):
        # 'are you recording?' / stop on an idle controller never touch the recorder or launch OBS.
        rec = DownRecorder(down_for=10 ** 6)
        c, spoken, notes = self._mk(rec)
        self.assertEqual(c.status_text(), "No, I'm not recording.")
        self.assertEqual(c.stop(), "not_recording")
        self.assertEqual(rec.calls, [])

    def test_autolaunch_disabled(self):
        # Config off -> OBS down just errors; no launch.
        rec = DownRecorder(down_for=10 ** 6)
        c, spoken, notes = self._mk(rec, autolaunch=False)
        c.start()
        c.join()
        self.assertNotIn("launch_obs", rec.calls)
        self.assertEqual(c.state, "idle")
        self.assertIn("isn't running", c.last_error)


class WindowControl(unittest.TestCase):
    """The 'Record screen' switch in chat_window.py, on a real (hidden) Tk window with the fake recorder."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"no display: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def build(self, monitors=(LEFT,), with_ctl=True):
        import chat_window as cw

        class Ov:
            face = None

        class App:
            state = {"chat_pos": [10, 10], "chat_size": [1200, 800]}
            working = False
            agents = {}
            overlay = Ov()

            def status(self):
                return ("Idle", "", None)

            def text_turn(self, *a):
                pass
        app = App()
        if with_ctl:
            app.screen_rec = sr.ScreenRecordController(FakeRecorder(monitors=list(monitors)))
        self.cw = cw.ChatWindow(self.root, app, single=True)
        return app

    def tearDown(self):
        try:
            self.cw.win.destroy()
        except Exception:
            pass

    def test_switch_exists_and_starts_off(self):
        # The control sits in the switch row and reads 'Record screen' while idle.
        self.build()
        self.assertEqual(self.cw.rec_lbl.lbl.cget("text"), "Record screen")
        self.assertFalse(self.cw.rec_lbl.get())

    def test_click_starts_then_shows_red_timer_then_stops(self):
        app = self.build()
        self.cw.rec_lbl.m.activate()                  # what a click does
        app.screen_rec.join()
        app.screen_rec.started_at -= 75
        self.cw._paint_record()
        self.assertTrue(self.cw.rec_lbl.get())
        self.assertEqual(self.cw.rec_lbl.lbl.cget("text"), "Recording 01:15")
        self.assertEqual(self.cw.rec_lbl.lbl.cget("fg"), "#E5484D")
        self.cw.rec_lbl.m.activate()                  # click again stops
        app.screen_rec.join()
        self.cw._paint_record()
        self.assertEqual(self.cw.rec_lbl.lbl.cget("text"), "Record screen")
        self.assertFalse(self.cw.rec_lbl.get())

    def test_chooser_lists_screens_and_pick_starts_that_one(self):
        app = self.build(monitors=[LEFT, MID, RIGHT])
        lab = sr.label_monitors([LEFT, MID, RIGHT])
        before = set(self.cw.win.winfo_children())
        self.cw.show_screen_chooser(lab)
        dlg = (set(self.cw.win.winfo_children()) - before).pop()
        texts = [w.cget("text") for w in dlg.winfo_children() if w.winfo_class() == "Label"]
        self.assertTrue(any(t.startswith("Left screen") and "1920x1080" in t for t in texts))
        self.assertTrue(any(t.startswith("Right screen") for t in texts))
        dlg.pick(2)                                   # what clicking the Right screen button calls
        app.screen_rec.join()
        self.assertIn(("start", 2), app.screen_rec.rec.calls)

    def test_switch_clears_when_obs_stopped_outside_app(self):
        # UI follows the controller after sync: red timer goes back to plain 'Record screen'.
        app = self.build()
        app.screen_rec.rec = StatusRecorder(live=True)
        self.cw.rec_lbl.m.activate()
        app.screen_rec.join()
        self.cw._paint_record()
        self.assertTrue(self.cw.rec_lbl.get())
        app.screen_rec.rec.live = False
        app.screen_rec.sync()
        self.cw._paint_record()
        self.assertEqual(self.cw.rec_lbl.lbl.cget("text"), "Record screen")
        self.assertFalse(self.cw.rec_lbl.get())

    def test_missing_controller_logs_error_not_crash(self):
        self.build(with_ctl=False)
        self.cw.toggle_record()
        self.cw._paint_record()
        self.assertEqual(self.cw.rec_lbl.lbl.cget("text"), "Record screen")


class StatusRecorder(FakeRecorder):
    """FakeRecorder plus status(); `live` is what OBS reports, `status_hook` runs inside the status call."""

    def __init__(self, live=False, seconds=0.0, ok=True, **kw):
        super().__init__(**kw)
        self.live, self.seconds, self.ok, self.status_hook = live, seconds, ok, None

    def status(self):
        self.calls.append("status")
        if self.status_hook:
            self.status_hook()
        if not self.ok:
            return {"ok": False, "connected": False, "obs_running": False, "recording": False, "seconds": 0.0,
                    "error": "Cannot reach OBS WebSocket."}
        return {"ok": True, "connected": True, "obs_running": True, "recording": self.live, "seconds": self.seconds}


class FollowObs(unittest.TestCase):
    """The controller follows OBS's real state (stopped/started outside the app, e.g. ObsRecorder.stop() elsewhere)."""

    def recording_ctl(self):
        rec = StatusRecorder(live=True)
        ctl = sr.ScreenRecordController(rec)
        ctl.start(source="ui")
        ctl.join()
        self.assertEqual(ctl.state, "recording")
        return ctl, rec

    def test_stopped_outside_app_goes_idle(self):
        # The 22:33 bug: OBS stopped out-of-band, app still showed 'Recording 01:37'.
        ctl, rec = self.recording_ctl()
        rec.live = False
        self.assertTrue(ctl.sync())
        self.assertEqual(ctl.state, "idle")
        self.assertEqual(ctl.elapsed(), 0)

    def test_still_recording_no_change(self):
        ctl, rec = self.recording_ctl()
        self.assertFalse(ctl.sync())
        self.assertEqual(ctl.state, "recording")

    def test_started_outside_app_shows_real_elapsed(self):
        # OBS recording something the app didn't start: show it, with OBS's own duration.
        rec = StatusRecorder(live=True, seconds=95.4)
        ctl = sr.ScreenRecordController(rec)
        self.assertTrue(ctl.sync())
        self.assertEqual(ctl.state, "recording")
        self.assertIn(ctl.elapsed(), (95, 96))

    def test_obs_absent_when_idle_is_harmless(self):
        ctl = sr.ScreenRecordController(StatusRecorder(ok=False))
        self.assertFalse(ctl.sync())
        self.assertEqual(ctl.state, "idle")

    def test_obs_gone_while_recording_goes_idle(self):
        ctl, rec = self.recording_ctl()
        rec.ok = False
        self.assertTrue(ctl.sync())
        self.assertEqual(ctl.state, "idle")

    def test_status_raising_is_swallowed(self):
        ctl, rec = self.recording_ctl()
        def boom():
            raise RuntimeError("x")
        rec.status_hook = boom
        self.assertFalse(ctl.sync())
        self.assertEqual(ctl.state, "recording")

    def test_recorder_without_status_is_ignored(self):
        ctl = sr.ScreenRecordController(FakeRecorder())
        self.assertFalse(ctl.sync())

    def test_does_not_touch_starting_state(self):
        # A start in progress (OBS not yet recording) must not be cancelled by the poll.
        rec = StatusRecorder(live=False)
        rec.gate = threading.Event()
        ctl = sr.ScreenRecordController(rec)
        ctl.start(source="ui")
        time.sleep(0.05)
        self.assertEqual(ctl.state, "starting")
        self.assertFalse(ctl.sync())
        self.assertEqual(ctl.state, "starting")
        self.assertNotIn("status", rec.calls)
        rec.gate.set()
        ctl.join()
        self.assertEqual(ctl.state, "recording")

    def test_stale_answer_discarded_if_start_began_mid_poll(self):
        # Poll reads 'not recording', meanwhile a start runs to completion: the stale answer must not reset it.
        rec = StatusRecorder(live=False)
        ctl = sr.ScreenRecordController(rec)
        def start_during_poll():
            rec.status_hook = None
            ctl.start(source="ui")
            ctl.join()
        rec.status_hook = start_during_poll
        self.assertFalse(ctl.sync())
        self.assertEqual(ctl.state, "recording")

    def test_toggle_still_works_after_sync(self):
        ctl, rec = self.recording_ctl()
        rec.live = False
        ctl.sync()
        self.assertEqual(ctl.toggle(source="ui"), "starting")      # idle again, so a click starts
        ctl.join()
        self.assertEqual(ctl.state, "recording")
        self.assertEqual(ctl.toggle(source="ui"), "stopping")
        ctl.join()
        self.assertEqual(ctl.state, "idle")

    def test_watch_thread_follows_obs(self):
        ctl, rec = self.recording_ctl()
        ctl.watch(0.05)
        ctl.watch(0.05)                                            # idempotent
        rec.live = False
        deadline = time.time() + 2
        while ctl.state != "idle" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(ctl.state, "idle")


class AudioParsing(unittest.TestCase):
    """Spoken audio options: 'with sound', 'with the microphone', both, none, and things that must NOT match."""

    def ex(self, text):
        return sr.parse_record_command_ex(text)

    def test_with_sound_is_pc_sound_and_mic(self):
        # The user, 10 Oct 22:46: bare 'with sound' means everything audible, his voice included.
        self.assertEqual(self.ex("start recording with sound"), ("start", None, {"desktop": True, "mic": True}))
        self.assertEqual(self.ex("record with audio")[2], {"desktop": True, "mic": True})

    def test_with_pc_sound_is_pc_sound_only(self):
        self.assertEqual(self.ex("start recording with pc sound")[2], {"desktop": True, "mic": False})

    def test_with_microphone_is_mic_only(self):
        self.assertEqual(self.ex("record with the microphone"), ("start", None, {"desktop": False, "mic": True}))
        self.assertEqual(self.ex("Start recording with the mic."), ("start", None, {"desktop": False, "mic": True}))

    def test_sound_and_mic_both(self):
        self.assertEqual(self.ex("record with sound and mic"), ("start", None, {"desktop": True, "mic": True}))
        self.assertEqual(self.ex("start recording with sound and the microphone")[2], {"desktop": True, "mic": True})

    def test_without_sound_is_silent(self):
        self.assertEqual(self.ex("record without sound"), ("start", None, {"desktop": False, "mic": False}))
        self.assertEqual(self.ex("start recording with no sound")[2], {"desktop": False, "mic": False})

    def test_without_mic_leaves_pc_sound_to_saved_choice(self):
        self.assertEqual(self.ex("start recording without the mic")[2], {"desktop": None, "mic": False})

    def test_screen_and_audio_together(self):
        self.assertEqual(self.ex("record screen two with the mic"), ("start", 2, {"desktop": False, "mic": True}))
        self.assertEqual(self.ex("start recording the left screen with sound"),
                         ("start", "left", {"desktop": True, "mic": True}))

    def test_plain_start_has_no_audio_opinion(self):
        # 'start recording' uses the saved defaults: audio is None, nothing passed on.
        self.assertEqual(self.ex("start recording"), ("start", None, None))
        self.assertEqual(sr.parse_record_command("start recording with sound"), ("start", None))   # 2-tuple API kept

    def test_negatives(self):
        for t in ("record the meeting with sound", "record a podcast with the microphone", "stop recording with sound",
                  "play the sound", "with sound", "turn the microphone on", "record music with sound and mic"):
            self.assertEqual(self.ex(t), (None, None, None), t)

    def test_controller_speaks_audio_and_passes_choice(self):
        c, spoken, notes, _ = make(monitors=[LEFT])
        c.rec.start = lambda monitor_index=None, monitor_indexes=None, **kw: (
            c.rec.calls.append(("start", monitor_index, kw)), {"ok": True, "error": "", "path": "", "audio": ["desktop"],
                                                              "warnings": []})[1]
        c.start(source="voice", audio_desktop=True, audio_mic=False)
        c.join()
        self.assertEqual(spoken, ["Recording the screen with PC sound."])
        self.assertIn(("start", 0, {"audio_desktop": True, "audio_mic": False}), c.rec.calls)
        self.assertEqual(c.audio, ["desktop"])

    def test_controller_audio_wordings_and_warning(self):
        for audio, want in ((["mic"], "with the microphone"), (["desktop", "mic"], "with PC sound and the microphone"),
                            ([], "with no sound")):
            self.assertEqual(sr.audio_phrase(audio), " " + want)
        self.assertEqual(sr.audio_phrase(None), "")
        c, spoken, notes, _ = make(monitors=[LEFT], start={"ok": True, "error": "", "audio": [],
                                                           "warnings": ["OBS wouldn't add the microphone (x)."]})
        c.start(source="voice")
        c.join()
        self.assertEqual(c.state, "recording")             # audio trouble never stops the video
        self.assertEqual(spoken, ["Recording the screen with no sound.", "OBS wouldn't add the microphone (x)."])

    def test_controller_without_audio_args_passes_nothing(self):
        c, *_ = make(monitors=[LEFT])
        c.start()
        c.join()
        self.assertEqual(c.rec.start_kw, {})

    def test_picker_options_passed_through_with_remember_false(self):
        c, *_ = make(monitors=[LEFT])
        c.start(monitors=[0], audio_desktop=False, audio_mic=True, record_dir="D:/r", mic_device="id1", remember=False)
        c.join()
        self.assertEqual(c.rec.start_kw, {"audio_desktop": False, "audio_mic": True, "record_dir": "D:/r",
                                          "mic_device": "id1", "remember": False})

    def test_dialog_options_defaults_for_plain_recorder(self):
        c, *_ = make(monitors=[LEFT])
        o = c.dialog_options()
        self.assertEqual(o["devices"], [{"id": "default", "name": "Default"}])
        self.assertEqual(o["prefs"], {})
        self.assertTrue(callable(o["warn"]))


class VoiceWiring(unittest.TestCase):
    """voice_claude.App._screen_record_command / _screen_choice, lifted out of the source (importing voice_claude would
    load the speech models). Proves: instant path, short replies, 'which screen?' follow-up, ordinary talk untouched."""

    @classmethod
    def setUpClass(cls):
        import ast
        import pathlib
        import re
        src = (pathlib.Path(__file__).parent / "voice_claude.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "App")
        fns = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in ("_screen_record_command", "_screen_choice")]
        mod = ast.Module(body=[ast.ClassDef(name="A", bases=[], keywords=[], body=fns, decorator_list=[])], type_ignores=[])
        ns = {"re": re, "time": time}
        exec(compile(ast.fix_missing_locations(mod), "voice_claude.App", "exec"), ns)
        cls.A = ns["A"]

    def app(self, monitors):
        import types
        a = self.A()
        a.said = []
        a.mouth = types.SimpleNamespace(say=a.said.append)
        a.pending_screen = 0.0
        a.screen_rec = sr.ScreenRecordController(FakeRecorder(monitors=list(monitors)), speak=a.said.append)
        a.screen_rec.choose = a._screen_choice
        return a

    def test_start_by_voice_replies_briefly(self):
        a = self.app([LEFT])
        self.assertTrue(a._screen_record_command("start recording"))
        a.screen_rec.join()
        self.assertEqual(a.said, ["Recording the screen."])

    def test_ordinary_sentence_is_not_handled(self):
        a = self.app([LEFT])
        self.assertFalse(a._screen_record_command("record the meeting notes in a file"))
        self.assertEqual(a.screen_rec.rec.calls, [])

    def test_which_screen_followup(self):
        a = self.app([LEFT, MID, RIGHT])
        self.assertTrue(a._screen_record_command("record the screen"))
        a.screen_rec.join()
        self.assertEqual(a.said, ["Which screen? left, middle or right?"])
        self.assertGreater(a.pending_screen, time.time())
        self.assertTrue(a._screen_record_command("the right one"))
        a.screen_rec.join()
        self.assertIn(("start", 2), a.screen_rec.rec.calls)
        self.assertEqual(a.said[-1], "Recording the right screen.")
        self.assertEqual(a.pending_screen, 0.0)

    def test_followup_expires_and_unrelated_talk_clears_it(self):
        a = self.app([LEFT, MID, RIGHT])
        a.pending_screen = time.time() - 1
        self.assertFalse(a._screen_record_command("left"))             # expired: 'left' is just a word now
        a.pending_screen = time.time() + 30
        self.assertFalse(a._screen_record_command("what time is it"))  # moved on: not handled, pending cleared
        self.assertEqual(a.pending_screen, 0.0)

    def test_cancel_followup(self):
        a = self.app([LEFT, MID])
        a.pending_screen = time.time() + 30
        self.assertTrue(a._screen_record_command("never mind"))
        self.assertEqual(a.said, ["Okay, not recording."])

    def test_stop_status_and_not_recording(self):
        a = self.app([LEFT])
        a._screen_record_command("stop recording")
        self.assertEqual(a.said[-1], "I'm not recording.")
        a._screen_record_command("are you recording?")
        self.assertEqual(a.said[-1], "No, I'm not recording.")
        a._screen_record_command("start recording")
        a.screen_rec.join()
        a._screen_record_command("stop recording")
        a.screen_rec.join()
        self.assertTrue(a.said[-1].startswith("Stopped, saved to"))

    def test_no_controller_means_not_handled(self):
        a = self.app([LEFT])
        a.screen_rec = None
        self.assertFalse(a._screen_record_command("start recording"))


class LabelKeepsRect(unittest.TestCase):
    def test_label_monitors_keeps_rect_so_picker_numbers_left_to_right(self):
        import screen_picker as sp
        obs_order = [{"index": 0, "name": "R", "rect": [1920, 0, 3840, 1080]},
                     {"index": 1, "name": "P", "rect": [0, 0, 1920, 1080]}]
        lab = sr.label_monitors(obs_order)
        self.assertEqual(lab[0]["rect"], [0, 0, 1920, 1080])
        rows = sp.build_rows(lab)
        self.assertEqual([(r["number"], r["index"]) for r in rows], [(1, 1), (2, 0)])   # Screen 1 = OBS index 1


if __name__ == "__main__":
    unittest.main()
