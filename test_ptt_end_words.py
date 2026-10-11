"""Talk-key end words (11 Oct 2026): 'send it' at the end of a talk-key turn sends it and is dropped from the text.
Pure tests of wake_word.ptt_clean / tail_status; the live auto-stop thread needs a mic and is not tested here."""
import re
import unittest

import wake_word

# Same pattern as voice_claude.CONFIRM_RE (importing voice_claude pulls in audio libraries).
CONFIRM_RE = re.compile(r"^(yes,? )?(confirmed?|confirm it|go ahead|do it|yes do it|send "
                        r"it|delete it|approved?)\b", re.I)


def ptt(text):
    return wake_word.ptt_clean(text, None, bool(CONFIRM_RE.match(text.strip())))


class TalkKeyEndWords(unittest.TestCase):
    def test_trailing_end_word_stripped(self):
        # the user's 00:19:37 turn: the end word must not reach Claude
        self.assertEqual(ptt("Open the projects folder, send it"), "Open the projects folder")
        self.assertEqual(ptt("check the logs, over."), "check the logs")
        self.assertEqual(ptt("make it blue, that's it"), "make it blue")

    def test_trailing_end_word_is_what_the_watcher_sends_on(self):
        # the auto-stop thread ends the recording when the tail judges 'end'
        self.assertEqual(wake_word.tail_status("make it blue, send it", None, cancel_words=[]), "end")
        self.assertNotEqual(wake_word.tail_status("send it to Bob and", None, cancel_words=[]), "end")

    def test_mid_sentence_left_alone(self):
        # 'send it' not at the end is part of the request
        self.assertEqual(ptt("send it to the printer please"), "send it to the printer please")

    def test_confirm_unchanged(self):
        # guard question answers: 'send it' / 'yes, send it' must reach CONFIRM_RE untouched
        self.assertEqual(ptt("send it"), "send it")
        self.assertEqual(ptt("Yes, send it."), "Yes, send it.")
        self.assertTrue(CONFIRM_RE.match(ptt("yes send it")))

    def test_end_word_alone_kept(self):
        # bare 'over' is not emptied into "Didn't catch that"
        self.assertEqual(ptt("over"), "over")

    def test_no_hallucination_stripping_on_ptt(self):
        # a sincere 'thank you' on the talk key is not touched
        self.assertEqual(ptt("thank you"), "thank you")

    def test_open_mic_path_unchanged(self):
        # open mic still uses clean_command as before
        self.assertEqual(wake_word.clean_command("open notepad, send it"), ("open notepad", False))
        self.assertEqual(wake_word.clean_command("send it"), ("", False))
        self.assertEqual(wake_word.clean_command("never mind"), ("", True))


def timed(text, gap_before_last=0.1, word_s=0.3):
    """Fake Whisper word timings: each word word_s long, gap_before_last of pause before the final word(s)."""
    out, t = [], 0.0
    ws = text.split()
    for i, w in enumerate(ws):
        out.append((" " + w, t, t + word_s))
        t += word_s + (gap_before_last if i == len(ws) - 2 else 0.05)
    return out


class EndWordSignOff(unittest.TestCase):
    """16:18 on 11 Oct: 'take all of this over' sent at 'over' mid-sentence."""

    def test_over_run_on_in_sentence_does_not_fire(self):
        w = timed("because I now want to take all of this over", gap_before_last=0.05)
        self.assertFalse(wake_word.end_word_sign_off(w, None, 0.5))

    def test_over_after_pause_fires(self):
        w = timed("check the logs over", gap_before_last=0.8)
        self.assertTrue(wake_word.end_word_sign_off(w, None, 0.5))

    def test_send_it_alone_fires(self):
        self.assertTrue(wake_word.end_word_sign_off([(" Send", 0.0, 0.3), (" it.", 0.3, 0.5)], None, 0.5))
        self.assertTrue(wake_word.end_word_sign_off([(" um", 0.0, 0.2), (" over", 0.3, 0.6)], None, 0.5))

    def test_send_it_after_pause_with_trailing_filler(self):
        w = [(" open", 0, .3), (" notepad,", .3, .7), (" send", 1.4, 1.6), (" it,", 1.6, 1.8), (" um.", 1.9, 2.0)]
        self.assertTrue(wake_word.end_word_sign_off(w, None, 0.5))

    def test_yeah_is_never_an_end_word(self):
        self.assertFalse(wake_word.end_word_sign_off(timed("yeah"), None, 0.5))
        self.assertFalse(wake_word.end_word_sign_off(timed("so yeah", gap_before_last=1.0), None, 0.5))
        self.assertNotEqual(wake_word.tail_status("yeah", None, cancel_words=[]), "end")

    def test_no_timings_defers_to_grace(self):
        self.assertIsNone(wake_word.end_word_sign_off([], None, 0.5))

    def test_full_sentence_not_stripped_when_not_fired(self):
        s = "because I now want to take all of this over"
        self.assertEqual(wake_word.ptt_strip_if_fired(s, False), s)
        self.assertEqual(wake_word.ptt_strip_if_fired(s + " to my work laptop", False), s + " to my work laptop")
        self.assertEqual(wake_word.ptt_strip_if_fired("check the logs, over.", True), "check the logs")
        # 'send it' is unambiguous: stripped even when he pressed the key himself
        self.assertEqual(wake_word.ptt_strip_if_fired("open notepad, send it", False), "open notepad")


if __name__ == "__main__":
    unittest.main()
