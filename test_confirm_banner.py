"""Tests for the 'Waiting for your "confirmed"' banner (confirm_state.py + its wiring). Fake clock, no Tk, no audio."""
import json
import pathlib
import re
import unittest

import chat_window as cw
import confirm_state as cs

HERE = pathlib.Path(__file__).resolve().parent
VOICE_SRC = (HERE / "voice_claude.py").read_text(encoding="utf-8-sig")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def hook_body(risk="command: git push origin main"):
    """What the safety hook prints, as it appears inside the JSON tool_result the app parses."""
    msg = ("BLOCKED until the user confirms by voice (" + risk + "). Stop here. In one short spoken sentence tell him "
           "exactly what you are about to do and ask him to say 'confirmed'.")
    return json.dumps(msg)


class TestTransitions(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.s = cs.ConfirmState(self.c)

    def test_idle_has_no_banner(self):
        self.assertFalse(self.s.active)
        self.assertIsNone(self.s.banner())

    def test_blocked_shows_banner_with_what(self):
        self.s.arm("git push origin main")
        state, detail = self.s.banner()
        self.assertEqual(state, "Confirm")
        self.assertEqual(detail, 'Waiting for your "confirmed": git push origin main')

    def test_blocked_then_confirmed_clears_at_once(self):
        self.s.arm("rm old.txt")
        self.s.clear()                      # App calls this when a confirmation is recorded
        self.assertIsNone(self.s.banner())

    def test_blocked_then_job_ends_clears(self):
        # do_task clears when the job ends; a reply that does not ask for 'confirmed' leaves it cleared
        self.s.arm("rm old.txt")
        self.s.clear()
        self.assertIsNone(cs.reply_asks_confirm("Done. All tests passed and the change is confirmed working."))
        self.assertIsNone(self.s.banner())

    def test_job_ends_but_reply_really_asks_so_it_rearms(self):
        self.s.arm("git push")
        self.s.clear()                      # job ended
        asked = cs.reply_asks_confirm("I'm about to push to GitHub. Say confirmed and I'll go ahead.")
        self.assertIn("Say confirmed", asked)
        self.s.arm(asked)
        self.assertIsNotNone(self.s.banner())

    def test_blocked_then_new_job_clears(self):
        self.s.arm("git push")
        self.s.clear()                      # do_task start
        self.assertFalse(self.s.active)

    def test_blocked_then_expiry_after_three_minutes(self):
        self.s.arm("git push")
        self.c.t += 179
        self.assertTrue(self.s.active)
        self.c.t += 2
        self.assertFalse(self.s.active)
        self.assertIsNone(self.s.banner())

    def test_rearm_restarts_the_window(self):
        self.s.arm("a")
        self.c.t += 170
        self.s.arm("b")
        self.c.t += 170
        self.assertEqual(self.s.banner()[1], 'Waiting for your "confirmed": b')

    def test_unknown_reason_still_says_what_it_knows(self):
        self.s.arm("")
        self.assertIn("safety check blocked something", self.s.banner()[1])


class TestParsing(unittest.TestCase):
    def test_describe_block_command(self):
        self.assertEqual(cs.describe_block(hook_body()), "git push origin main")

    def test_describe_block_tool(self):
        self.assertEqual(cs.describe_block(hook_body("tool: mcp__slack__send_message")), "mcp__slack__send_message")

    def test_describe_block_with_escaped_quotes_and_long_command(self):
        body = hook_body('command: Remove-Item "C:\\x\\' + "y" * 200 + '"')
        out = cs.describe_block(body)
        self.assertTrue(out.startswith("Remove-Item"))
        self.assertLessEqual(len(out), 70)

    def test_describe_block_no_reason_or_garbage(self):
        for b in ("", None, "BLOCKED until the user confirms", "something else"):
            self.assertEqual(cs.describe_block(b), "", repr(b))

    def test_reply_asks_confirm_true_cases(self):
        for r in ("Say confirmed to delete the folder.", "I need your confirmed before I send it.",
                  "Waiting for your confirmation.", "Please confirm and I'll push.",
                  "Ready. Just say 'confirmed' and it goes."):
            self.assertIsNotNone(cs.reply_asks_confirm(r), r)

    def test_reply_asks_confirm_false_cases(self):
        # these used to light the banner because the old test was just \bconfirm
        for r in ("Confirmed, the build passed.", "I confirmed the file exists.", "All good.", "", None,
                  "The confirmation was recorded earlier and the push went through."):
            self.assertIsNone(cs.reply_asks_confirm(r), repr(r))


class TestWiring(unittest.TestCase):
    def test_every_path_in_the_app(self):
        # confirmation recorded (spoken and typed), job start, job end, blocked event and status all use the state
        for needle in ("confirm_state.describe_block(body)", "self.confirm.arm(info)", "confirm_state.reply_asks_confirm(reply)",
                       "b = self.confirm.banner()"):
            self.assertIn(needle, VOICE_SRC)
        self.assertGreaterEqual(VOICE_SRC.count("self.confirm.clear()"), 4)
        # the old sticky behaviours are gone
        self.assertNotIn("Say 'confirmed' to go ahead, or tell it not to.", VOICE_SRC)
        self.assertNotRegex(VOICE_SRC, r're\.search\(r"\\bconfirm", reply')

    def test_banner_is_amber_only_while_blocked(self):
        self.assertEqual(cw.STATUS_COLOURS["Confirm"], cw.PALETTE["system"][0])
        for state in ("Idle", "Working", "Speaking", "Listening"):
            self.assertNotEqual(cw.STATUS_COLOURS[state], cw.STATUS_COLOURS["Confirm"])
        src = (HERE / "chat_window.py").read_text(encoding="utf-8-sig")
        self.assertRegex(src, r'fg=PALETTE\["system"\]\[0\] if state == "Confirm" else FG')


class TestOrdinaryQuestion(unittest.TestCase):
    """the user's screenshot: 'Waiting for you / Say confirmed...' showed after a normal reply. A plain question must
    now give a neutral 'Your turn' with the question itself and never the word confirmed."""
    def setUp(self):
        self.c = Clock()
        self.s = cs.ConfirmState(self.c)

    def test_question_gives_neutral_banner(self):
        q = cs.reply_question("Done, it's in the Projects folder. Want me to open it in VS Code?")
        self.s.ask(q)
        self.assertEqual(self.s.banner(), ("Waiting", "Want me to open it in VS Code?"))
        self.assertNotIn("confirm", self.s.banner()[1].lower())

    def test_non_question_replies_arm_nothing(self):
        for r in ("Done.", "Opened it.", "", None, "I wasn't sure? Anyway it's done."):
            self.assertIsNone(cs.reply_question(r), repr(r))

    def test_long_question_is_clipped(self):
        q = cs.reply_question("Which of the " + "very " * 40 + "long options do you want?")
        self.assertLessEqual(len(q), 60)

    def test_question_clears_when_don_speaks_or_types(self):
        self.s.ask("Shall I go on?")
        self.s.clear()                      # handle_audio / text_turn
        self.assertIsNone(self.s.banner())

    def test_question_expires_after_three_minutes(self):
        self.s.ask("Shall I go on?")
        self.c.t += 181
        self.assertIsNone(self.s.banner())

    def test_confirm_hint_gone_after_confirmation(self):
        self.s.arm("git push")
        self.assertIn("confirmed", self.s.banner()[1])
        self.s.clear()
        self.assertIsNone(self.s.banner())

    def test_block_after_question_replaces_it_with_the_confirm_banner(self):
        self.s.ask("Shall I go on?")
        self.s.arm("git push")
        self.assertEqual(self.s.banner(), ("Confirm", 'Waiting for your "confirmed": git push'))

    def test_exact_subtexts(self):
        self.s.arm("")
        self.assertEqual(self.s.banner()[1], 'Waiting for your "confirmed" (the safety check blocked something)')
        self.s.ask("")
        self.assertEqual(self.s.banner(), ("Waiting", "Your turn"))

    def test_window_wording_and_colours(self):
        src = (HERE / "chat_window.py").read_text(encoding="utf-8-sig")
        self.assertIn('{"Confirm": "Waiting for you", "Waiting": "Your turn"}', src)
        self.assertNotEqual(cw.STATUS_COLOURS["Waiting"], cw.STATUS_COLOURS["Confirm"], "question is not the warning colour")
        self.assertIn("confirm_state.reply_question(reply)", VOICE_SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
