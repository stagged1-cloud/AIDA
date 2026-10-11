"""Tests for echo_guard.py: the half-duplex gate and the self-text filter. Run: .venv\Scripts\python -m unittest test_echo_gate -v"""
import unittest

import numpy as np

from echo_guard import EchoGate, SpokenLog


class Clock:
    """Fake clock so timing tests are exact and instant."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeMouth:
    def __init__(self):
        self.speaking = False


class FakeEars:
    """Mirrors the gating branch of Ears.start().on_audio without any hardware."""
    def __init__(self, gate):
        self.gate, self.frames = gate, []

    def on_audio(self, data):
        if self.gate is not None and self.gate.blocked():
            self.frames.append(np.zeros_like(data))
        else:
            self.frames.append(data.copy())


def make(tail=0.6, enabled=True):
    c, m = Clock(), FakeMouth()
    return c, m, EchoGate(lambda: m.speaking, tail_s=tail, enabled=enabled, clock=c)


# ---------------------------------------------------------------- gate
class GateTests(unittest.TestCase):
    def test_gate_open_when_silent(self):
        # nothing playing, nothing recently played: mic passes
        c, m, g = make()
        assert not g.blocked()


    def test_gate_blocks_while_speaking(self):
        # AIDA talking: audio must be blocked
        c, m, g = make()
        m.speaking = True
        assert g.blocked()


    def test_gate_tail_window(self):
        # after speech ends the gate stays shut for the tail, then opens
        c, m, g = make(tail=0.6)
        m.speaking = True
        assert g.blocked()
        m.speaking = False
        c.t += 0.3
        assert g.blocked()          # inside the 600 ms tail
        c.t += 0.29
        assert g.blocked()          # 590 ms: still inside
        c.t += 0.02
        assert not g.blocked()      # 610 ms: open


    def test_gate_disabled(self):
        # echo_gate: false means never block, even mid-speech
        c, m, g = make(enabled=False)
        m.speaking = True
        assert not g.blocked()


    def test_hotkey_barge_in_bypasses_gate(self):
        # The user presses ` while AIDA talks: the gate opens at once, even though Mouth is still winding down
        c, m, g = make()
        m.speaking = True
        assert g.blocked()
        g.barge_in()
        assert not g.blocked()
        c.t += 0.5
        assert not g.blocked()      # still within the bypass hold


    def test_barge_in_then_new_speech_is_gated_again(self):
        # after the bypass hold, fresh AIDA speech during the same recording is blanked again
        c, m, g = make()
        g.barge_in()
        c.t += 1.0
        m.speaking = True
        assert g.blocked()


    def test_barge_in_clears_tail(self):
        # speech just ended (tail active), the user presses the hotkey: no tail applies
        c, m, g = make()
        m.speaking = True
        g.blocked()
        m.speaking = False
        g.barge_in()
        assert not g.blocked()


    def test_ears_blank_frames_keep_timing(self):
        # gated audio becomes zeros of the same length, so the recording keeps its duration
        c, m, g = make()
        e = FakeEars(g)
        chunk = np.ones((160, 1), dtype=np.float32)
        e.on_audio(chunk)                       # silent AIDA: real audio
        m.speaking = True
        e.on_audio(chunk)                       # speaking: zeros
        m.speaking = False
        c.t += 5
        e.on_audio(chunk)                       # long after: real audio
        assert [float(f.sum()) for f in e.frames] == [160.0, 0.0, 160.0]
        assert all(f.shape == chunk.shape for f in e.frames)


    def test_ears_without_gate_unchanged(self):
        # gate=None (feature off or not wired) leaves audio untouched
        e = FakeEars(None)
        e.on_audio(np.ones((10, 1), dtype=np.float32))
        assert e.frames[0].sum() == 10




# ---------------------------------------------------------------- self-text filter
def spoken(*texts):
    c = Clock()
    s = SpokenLog(window_s=90, min_block=4, clock=c)
    for t in texts:
        s.add(t)
    return c, s


REAL_REPLY = ("The three reports are in. The code is already clean. The only leftover is a small face patch, "
              "and fixing it would trigger a package reinstall, so I'd skip it and apply only the harmless code tidy-ups.")



class FilterTests(unittest.TestCase):
    def test_exact_echo_dropped(self):
        # the whole transcript is AIDA's own words: dropped entirely
        c, s = spoken(REAL_REPLY)
        out, changed = s.clean("The only leftover is a small face patch and fixing it would trigger a package reinstall")
        assert changed and out == ""


    def test_partial_echo_plus_confirmed(self):
        # AIDA words followed by the user's real word: only the real word survives
        c, s = spoken("Say it again and I will do the thing and then tell you when the whole job is finished.")
        out, changed = s.clean("do the thing and then tell you when confirmed")
        assert changed and out == "confirmed"


    def test_replay_real_1735_example(self):
        # the real incident: transcript 'so, so i'd skip it and I'm concerned' after AIDA said '...so I'd skip it...'
        c, s = spoken(REAL_REPLY)
        out, changed = s.clean("so, so i'd skip it and I'm concerned")
        assert changed
        assert "skip" not in out.lower()
        assert out.lower().endswith("concerned")


    def test_replay_longer_echo_fully_dropped(self):
        # a longer capture of the same reply (even with a recogniser slip mid-way) is ignored
        c, s = spoken(REAL_REPLY)
        out, changed = s.clean("the only leftover is a small face patch and fixing would trigger a package reinstall so I'd skip it")
        assert changed and out == ""


    def test_short_command_words_always_pass(self):
        # guard words and voice commands must never be eaten, even if AIDA just said them
        c, s = spoken("Say confirmed to go ahead, or say stop, cancel, yes, repeat that.")
        for w in ["confirmed", "stop", "yes", "cancel", "repeat that", "Confirmed.", "yes go ahead", "shut up"]:
            out, changed = s.clean(w)
            assert not changed and out == w


    def test_command_prefix_short_passes(self):
        # 'confirmed' plus a few words that happen to be in AIDA's last sentence is still the user
        c, s = spoken("Want me to run it now?")
        out, changed = s.clean("yes run it now")
        assert not changed and out == "yes run it now"


    def test_empty_and_none_input(self):
        # nothing in, nothing out, nothing flagged
        c, s = spoken(REAL_REPLY)
        assert s.clean("") == ("", False)
        assert s.clean(None) == (None, False)
        assert s.clean("   ") == ("   ", False)


    def test_nothing_spoken_yet(self):
        # empty record: transcript untouched
        c, s = spoken()
        text = "open chrome and go to the BBC news page please"
        assert s.clean(text) == (text, False)


    def test_unicode_input(self):
        # smart apostrophes, accents, emoji must not crash or mangle
        c, s = spoken("It’s fine, I’d skip the café patch and move on to the next job today.")
        out, changed = s.clean("I’d skip the café patch and move on")
        assert changed and out == ""
        out, changed = s.clean("naïve résumé 😀 über")
        assert not changed and out == "naïve résumé 😀 über"


    def test_similar_but_different_don_sentence_kept(self):
        # The user says something that shares a few words with AIDA but no 4-word run: untouched
        c, s = spoken(REAL_REPLY)
        text = "I think the code is clean but I would still skip the package reinstall tonight"
        out, changed = s.clean(text)
        assert not changed and out == text


    def test_old_speech_expires(self):
        # AIDA's words from more than 90 s ago no longer count
        c, s = spoken("The only leftover is a small face patch and fixing it needs a reinstall.")
        c.t += 120
        text = "the only leftover is a small face patch"
        assert s.clean(text) == (text, False)


    def test_other_speech_after_echo_kept(self):
        # echo at the front, then a genuine the user request: the request survives
        c, s = spoken("The three reports are in and the code is already clean.")
        out, changed = s.clean("the three reports are in and the code is already clean open Chrome please")
        assert changed and out == "open Chrome please"


# ---------------------------------------------------------------- echo_possible (11 Oct 00:12:08 fix)
class TestEchoPossible(unittest.TestCase):
    """The text filter must only run when AIDA could really be in the recording. Before this, the user quoting
    AIDA back ('your next long ramble is the real test of the speech hold') was deleted as echo, so it looked
    like he had been cut off mid-sentence."""

    def test_quiet_aida_means_no_echo_possible(self):
        # AIDA last spoke 30 s ago; the user then records 5 s: nothing of AIDA can be in it -> don't filter
        c, m, g = make()
        m.speaking = True; g.blocked()
        m.speaking = False; c.t += 30
        assert not g.echo_possible(5.0)

    def test_aida_speaking_during_capture_means_echo_possible(self):
        # AIDA stopped 3 s ago, the user's recording is 5 s long: the start of it overlaps AIDA -> filter
        c, m, g = make()
        m.speaking = True; g.blocked()
        m.speaking = False; c.t += 3
        assert g.echo_possible(5.0)

    def test_margin_covers_speaker_tail(self):
        # AIDA stopped 1 s before a 0.5 s capture began: within the 1.5 s margin -> filter
        c, m, g = make()
        m.speaking = True; g.blocked()
        m.speaking = False; c.t += 1.5
        assert g.echo_possible(0.5)
        c.t += 2
        assert not g.echo_possible(0.5)

    def test_barge_in_still_counts_as_audible(self):
        # talk key pressed over AIDA: barge_in opens the gate but the moment is still remembered
        c, m, g = make()
        m.speaking = True
        g.echo_possible(0)            # what on_toggle does before barge_in
        g.barge_in()
        m.speaking = False; c.t += 2
        assert g.echo_possible(4.0)

    def test_tracked_even_with_gate_disabled(self):
        c, m, g = make(enabled=False)
        m.speaking = True; g.blocked()
        m.speaking = False; c.t += 1
        assert g.echo_possible(2.0)


if __name__ == '__main__':
    unittest.main()
