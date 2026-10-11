import io
import unittest

import live_commentary as lc


def jpeg(w=320, h=200):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(out, "JPEG")
    return out.getvalue()


class FakeWatch:
    def __init__(self):
        self.frames = {1: None, 2: None}

    def put(self, mon, ts, data=None):
        self.frames[mon] = {"monitor": mon, "ts": ts, "jpeg": data or jpeg()}

    def monitor_rects(self):
        return {m: None for m in self.frames}

    def get_latest_frame(self, mon):
        return self.frames.get(mon)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(replies=None, **cfg):
    c = {"live_commentary_enabled": True, "live_commentary_interval_s": 20, "live_commentary_min_gap_s": 45}
    c.update(cfg)
    clock, watch = Clock(), FakeWatch()
    said, calls = [], []
    replies = list(replies or [])
    state = {"muted": False, "busy": ""}

    def look(data, text):
        calls.append(text)
        return replies.pop(0) if replies else "SILENT"

    obj = lc.LiveCommentary(c, watch, look, said.append, muted_fn=lambda: state["muted"],
                            busy_fn=lambda: state["busy"], clock=clock)
    return obj, clock, watch, said, calls, state


class TestCommentary(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(lc.LiveCommentary({}, FakeWatch(), None, None).enabled)
        self.assertEqual(lc.LiveCommentary({}, FakeWatch(), None, None).tick(), "off")

    def test_decision_log(self):
        o, clock, w, said, calls, state = make(["DEATH: Lovely corpse run."])
        logs = []
        o.log = lambda who, text: logs.append(text)
        self.assertEqual(o.tick(), "nochange")
        self.assertIn("first decision nochange", logs[-1])
        state["busy"] = "turn running"
        clock.t += 31
        self.assertEqual(o.tick(), "busy")
        self.assertIn("last decision busy (turn running)", logs[-1])
        state["busy"] = ""
        w.put(1, 5.0)
        clock.t += 1
        self.assertEqual(o.tick(), "death")
        self.assertTrue(any("commentary look" in l and "DEATH: Lovely corpse run." in l for l in logs))

    def test_interval(self):
        o, clock, w, said, calls, _ = make(["Nice hat.", "Lovely weather."])
        w.put(1, 1.0)
        self.assertEqual(o.tick(), "spoke")
        w.put(1, 2.0)
        clock.t += 10
        self.assertEqual(o.tick(), "interval")
        self.assertEqual(len(calls), 1)

    def test_change_skip(self):
        o, clock, w, said, calls, _ = make(["Nice hat."])
        w.put(1, 1.0)
        o.tick()
        clock.t += 25
        self.assertEqual(o.tick(), "nochange")     # same ring frame: no call
        self.assertEqual(len(calls), 1)
        clock.t += 25
        w.put(2, 5.0)
        self.assertEqual(o.tick(), "silent")
        self.assertEqual(len(calls), 2)

    def test_picks_newest_monitor(self):
        o, clock, w, said, calls, _ = make()
        w.put(1, 3.0)
        w.put(2, 7.0)
        o.tick()
        self.assertEqual(o.last_frame_ts, {2: 7.0})

    def test_hourly_cap(self):
        o, clock, w, said, calls, _ = make(live_commentary_max_per_hour=2, live_commentary_interval_s=1)
        for i in range(4):
            w.put(1, float(i + 1))
            clock.t += 2
            o.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(o.counters["skipped_cap"], 2)
        clock.t += 3700                                # an hour later the cap clears
        w.put(1, 99.0)
        o.tick()
        self.assertEqual(len(calls), 3)

    def test_daily_cap(self):
        o, clock, w, said, calls, _ = make(live_commentary_max_per_day=1, live_commentary_interval_s=1)
        w.put(1, 1.0)
        o.tick()
        clock.t += 5
        w.put(1, 2.0)
        self.assertEqual(o.tick(), "cap")

    def test_min_gap(self):
        o, clock, w, said, calls, _ = make(["First line.", "Second line here.", "Third, totally different."])
        w.put(1, 1.0)
        o.tick()
        clock.t += 21
        w.put(1, 2.0)
        self.assertEqual(o.tick(), "gap")          # model spoke but 21 s < 45 s gap
        clock.t += 30
        w.put(1, 3.0)
        self.assertEqual(o.tick(), "spoke")
        self.assertEqual(said, ["First line.", "Third, totally different."])

    def test_death_beats_gap(self):
        o, clock, w, said, calls, _ = make(["First line.", "DEATH: Ha. Nice corpse run, champ."])
        w.put(1, 1.0)
        o.tick()
        clock.t += 21
        w.put(1, 2.0)
        self.assertEqual(o.tick(), "death")
        self.assertEqual(said[-1], "Ha. Nice corpse run, champ.")

    def test_death_once_per_death(self):
        o, clock, w, said, calls, _ = make(["DEATH: Ha ha, you died, sucker.", "DEATH: Dead again, genius.",
                                            "SILENT", "DEATH: Ha, dead once more."])
        logs = []
        o.log = lambda who, text: logs.append(text)
        for i, want in enumerate(["death", "death_dup", "silent", "death"]):
            w.put(1, float(i + 1))
            clock.t += 25
            self.assertEqual(o.tick(), want)
        self.assertEqual(said, ["Ha ha, you died, sucker.", "Ha, dead once more."])
        self.assertTrue(any("same death already reported" in l for l in logs))

    def test_death_cooldown_expires(self):
        o, clock, w, said, calls, _ = make(["DEATH: One.", "DEATH: Two."], live_commentary_death_cooldown_s=60)
        w.put(1, 1.0)
        self.assertEqual(o.tick(), "death")
        clock.t += 61
        w.put(1, 2.0)
        self.assertEqual(o.tick(), "death")

    def test_prompt_forbids_invention(self):
        p = lc.SYSTEM_PROMPT
        self.assertIn("NEVER invent", p)
        self.assertIn("you died, sucker", p)

    def test_silent(self):
        for r in ("SILENT", "silent.", "", "  SILENT\n"):
            o, clock, w, said, calls, _ = make([r])
            w.put(1, 1.0)
            self.assertEqual(o.tick(), "silent")
            self.assertEqual(said, [])

    def test_repeat_suppressed(self):
        o, clock, w, said, calls, _ = make(["You died again.", "You died again!"], live_commentary_min_gap_s=0,
                                           live_commentary_death_gap_s=0)
        w.put(1, 1.0)
        o.tick()
        clock.t += 21
        w.put(1, 2.0)
        self.assertEqual(o.tick(), "repeat")
        self.assertEqual(said, ["You died again."])
        self.assertIn("You died again.", calls[-1])   # last lines are fed back to the model

    def test_muted_no_call(self):
        o, clock, w, said, calls, st = make(["Hi."])
        st["muted"] = True
        w.put(1, 1.0)
        self.assertEqual(o.tick(), "muted")
        self.assertEqual(calls, [])

    def test_busy_no_call_and_no_interrupt(self):
        o, clock, w, said, calls, st = make(["Hi there."])
        st["busy"] = "user talking"
        w.put(1, 1.0)
        self.assertEqual(o.tick(), "busy")
        self.assertEqual(calls, [])

        def look(data, text):                       # The user starts talking during the call
            st["busy"] = "turn running"
            return "Something to say."
        st["busy"] = ""
        o.look = look
        self.assertEqual(o.tick(), "busy")
        self.assertEqual(said, [])

    def test_word_cap_and_markdown(self):
        line, death = lc.parse_reply("**" + " ".join(["word"] * 30) + "**")
        self.assertEqual(len(line.split()), 15)
        self.assertNotIn("*", line)
        self.assertFalse(death)

    def test_look_error_logged(self):
        o, clock, w, said, calls, _ = make()

        def boom(d, t):
            raise RuntimeError("down")
        o.look = boom
        w.put(1, 1.0)
        self.assertEqual(o.tick(), "error")

    def test_fit_jpeg(self):
        from PIL import Image
        out = lc.fit_jpeg(jpeg(2560, 1440))
        self.assertEqual(Image.open(io.BytesIO(out)).size, (960, 540))
        small = jpeg(800, 450)
        self.assertIs(lc.fit_jpeg(small), small)

    def test_claude_look_slim_flags(self):
        from unittest import mock
        res = '{"type":"result","is_error":false,"result":"SILENT"}'
        with mock.patch.object(lc.subprocess, "run", return_value=mock.Mock(stdout=res, stderr="", returncode=0)) as run:
            self.assertEqual(lc.ClaudeLook("claude", "m")(b"x", "hi"), "SILENT")
        cmd, kw = run.call_args[0][0], run.call_args[1]
        for flag in ("--tools", "--disable-slash-commands", "--setting-sources", "--strict-mcp-config",
                     "--no-session-persistence", "--system-prompt"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(kw["env"]["MAX_THINKING_TOKENS"], "0")

    def test_commands(self):
        self.assertTrue(lc.parse_command("commentary on"))
        self.assertFalse(lc.parse_command("commentary off."))
        self.assertTrue(lc.parse_command("turn on the commentary"))
        self.assertFalse(lc.parse_command("turn live commentary off"))
        self.assertIsNone(lc.parse_command("what's the commentary on the match"))

    def test_set_enabled(self):
        o = lc.LiveCommentary({}, FakeWatch(), lambda d, t: "SILENT", lambda s: None)
        o.start = lambda: None
        o.set_enabled(True)
        self.assertTrue(o.enabled)
        o.set_enabled(False)
        self.assertEqual(o.tick(), "off")


if __name__ == "__main__":
    unittest.main()
