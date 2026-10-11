"""Tests for accepted mishearings of the safety word 'confirmed' (confirm_state.is_confirm_variant + voice_claude wiring)."""
import pathlib
import re
import unittest

import confirm_state as cs

SRC = (pathlib.Path(__file__).parent / "voice_claude.py").read_text(encoding="utf-8")


class Positives(unittest.TestCase):
    def test_each_variant_bare_and_with_punctuation(self):
        # every listed word, as the whole utterance, with comma / full stop / none, any case
        for w in cs.CONFIRM_WORD_VARIANTS:
            for form in (w, w.upper(), w.capitalize(), w.capitalize() + ".", w + ",", w + "!", "  " + w + "  "):
                self.assertTrue(cs.is_confirm_variant(form), form)

    def test_variant_with_trailing_instruction(self):
        # punctuation or a continuation word after the variant, then any instruction
        for t in ("confirmed, and then delete the screenshots", "Confound, skip it", "Concerned. Do that now",
                  "Confined and push", "confirm then carry on", "Conformed do it", "Confirmation, go ahead"):
            self.assertTrue(cs.is_confirm_variant(t), t)

    def test_list_is_exactly_the_agreed_one(self):
        self.assertEqual(set(cs.CONFIRM_WORD_VARIANTS),
                         {"confirmed", "confirm", "confined", "confound", "concerned", "conformed", "confirmation"})


class Negatives(unittest.TestCase):
    def test_long_sentences_starting_with_variant(self):
        for t in ("Concerned about the backup job running overnight on the other laptop",
                  "Concerned about this", "Confirm the booking with the plumber tomorrow please",
                  "Confirmation email arrived from the bank"):
            self.assertFalse(cs.is_confirm_variant(t), t)

    def test_word_later_in_sentence(self):
        for t in ("I confirm that", "I am concerned about this", "please confirmed", "yes confirmed", "no, confound it"):
            self.assertFalse(cs.is_confirm_variant(t), t)

    def test_empty_none_and_non_strings(self):
        for t in ("", "   ", None, 5, [], "...", ","):
            self.assertFalse(cs.is_confirm_variant(t), repr(t))

    def test_no_fuzzy_or_substring(self):
        for t in ("confirmd", "confirmedly", "unconfirmed", "confirms", "conformist", "confused", "confind", "config"):
            self.assertFalse(cs.is_confirm_variant(t), t)


class Wiring(unittest.TestCase):
    def test_both_paths_use_it_and_same_three_minute_window(self):
        # spoken path and typed path each call the matcher and write the token as now+180, as before
        self.assertEqual(SRC.count("confirm_state.is_confirm_variant(text.strip())"), 2)
        for m in re.finditer(r"if CONFIRM_RE\.match\(text\.strip\(\)\) or confirm_state\.is_confirm_variant", SRC):
            block = SRC[m.start():m.start() + 700]
            self.assertIn("TOKEN_FILE.write_text(str(time.time() + 180)", block)
        self.assertEqual(cs.WINDOW_S, 180)

    def test_original_regex_untouched(self):
        self.assertIn(r'CONFIRM_RE = re.compile(r"^(yes,? )?(confirmed?|confirm it|go ahead|do it|yes do it|send it|delete it|approved?)\b", re.I)', SRC)

    def test_not_used_on_helper_output(self):
        # the matcher is only called in the two the user-input paths
        self.assertEqual(SRC.count("is_confirm_variant"), 2)


if __name__ == "__main__":
    unittest.main()
