"""Tests for sounds.py (generated WAV beeps/chime) and its wiring. No real audio: winsound is faked."""
import ast
import pathlib
import re
import tempfile
import unittest
import wave

import sounds

HERE = pathlib.Path(__file__).resolve().parent
VOICE_SRC = (HERE / "voice_claude.py").read_text(encoding="utf-8-sig")


class FakePlayer:
    """Stands in for the winsound module."""
    SND_FILENAME, SND_ASYNC = 0x20000, 0x1

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def PlaySound(self, path, flags):
        if self.fail:
            raise self.fail
        self.calls.append((pathlib.Path(path).stem, flags))


def make(enabled=True, speaking=False, fail=None):
    logs = []
    d = tempfile.mkdtemp()
    p = FakePlayer(fail)
    s = sounds.Sounds(d, enabled=enabled, speaking=lambda: speaking, log=lambda w, t: logs.append((w, t)), player=p)
    return s, p, logs, d


class TestWav(unittest.TestCase):
    def test_all_four_files_are_valid_wavs(self):
        # header: mono, 16-bit, 22.05 kHz, non-empty
        s, _, _, d = make()
        s.ensure()
        for kind in ("start", "stop", "error", "chime"):
            with wave.open(str(pathlib.Path(d) / f"{kind}.wav")) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, sounds.RATE), kind)
                self.assertGreater(w.getnframes(), 0)

    def test_lengths_match_the_old_beeps(self):
        # start 90 ms, error 200 ms, chime 60+80 ms; plus small gaps/tail (< 60 ms extra)
        s, _, _, d = make()
        s.ensure()
        want = {"start": 90, "stop": 90, "error": 200, "chime": 140}
        for kind, ms in want.items():
            with wave.open(str(pathlib.Path(d) / f"{kind}.wav")) as w:
                got = 1000 * w.getnframes() / w.getframerate()
            self.assertTrue(ms <= got <= ms + 60, f"{kind}: {got:.0f} ms")

    def test_no_clipping_and_soft_edges(self):
        # peak well below full scale; first and last samples are ~0 so there is no click
        s, _, _, d = make()
        s.ensure()
        for kind in sounds.SPECS:
            with wave.open(str(pathlib.Path(d) / f"{kind}.wav")) as w:
                raw = w.readframes(w.getnframes())
            import struct
            xs = struct.unpack("<%dh" % (len(raw) // 2), raw)
            self.assertLess(max(abs(x) for x in xs), 32767 * 0.5, kind)
            self.assertGreater(max(abs(x) for x in xs), 3000, "audible, not silent")
            self.assertEqual(xs[0], 0)
            self.assertEqual(xs[-1], 0)

    def test_ensure_is_idempotent_and_repairs_empty_file(self):
        s, _, _, d = make()
        s.ensure()
        p = pathlib.Path(d) / "chime.wav"
        p.write_bytes(b"")
        s.ensure()
        self.assertGreater(p.stat().st_size, 100)


class TestPlay(unittest.TestCase):
    def test_plays_through_playsound_async_file(self):
        s, p, logs, _ = make()
        self.assertTrue(s.play("start"))
        self.assertEqual(p.calls, [("start", FakePlayer.SND_FILENAME | FakePlayer.SND_ASYNC)])
        self.assertEqual(logs, [])

    def test_chime_and_error_skipped_while_speaking(self):
        s, p, _, _ = make(speaking=True)
        self.assertFalse(s.play("chime"))
        self.assertFalse(s.play("error"))
        self.assertEqual(p.calls, [])

    def test_start_and_stop_beeps_still_play_while_speaking(self):
        # they follow a hotkey press that has just silenced AIDA
        s, p, _, _ = make(speaking=True)
        self.assertTrue(s.play("start"))
        self.assertTrue(s.play("stop"))
        self.assertEqual([c[0] for c in p.calls], ["start", "stop"])

    def test_config_switch_off_is_silent(self):
        s, p, _, _ = make(enabled=False)
        for k in sounds.SPECS:
            self.assertFalse(s.play(k))
        self.assertEqual(p.calls, [])

    def test_force_ignores_switch_and_speaking(self):
        # the "Play test sounds" path
        s, p, _, _ = make(enabled=False, speaking=True)
        self.assertTrue(s.play("chime", force=True))
        self.assertEqual(len(p.calls), 1)

    def test_unknown_kind_is_ignored(self):
        s, p, logs, _ = make()
        self.assertFalse(s.play("nonsense"))
        self.assertEqual((p.calls, logs), ([], []))

    def test_failure_is_logged_once_per_sound_not_raised(self):
        s, _, logs, _ = make(fail=OSError("no device"))
        for _ in range(3):
            self.assertFalse(s.play("chime"))
        self.assertEqual(logs, [("error", "sound: chime failed: no device")])
        s.play("start")
        self.assertEqual(len(logs), 2, "a different sound reports separately")

    def test_speaking_callback_that_raises_does_not_break_play(self):
        s, p, _, _ = make()
        s.speaking = lambda: 1 / 0
        self.assertTrue(s.play("chime"))

    def test_broken_log_callback_is_swallowed(self):
        s, _, _, _ = make(fail=OSError("x"))
        s.log = lambda w, t: 1 / 0
        self.assertFalse(s.play("start"))


class TestWiring(unittest.TestCase):
    def test_beep_and_chime_use_sounds_and_no_winsound_beep(self):
        self.assertNotRegex(VOICE_SRC, r"winsound\.Beep\(")
        self.assertIn('enabled=bool(CFG.get("sounds", True))', VOICE_SRC)

    def test_config_key_exists_and_defaults_true(self):
        cfg = (HERE / "config.example.yaml").read_text(encoding="utf-8-sig")
        self.assertRegex(cfg, r"(?m)^sounds: true\b")

    def test_voice_command_and_menu_item(self):
        self.assertIn("test sounds?", VOICE_SRC)
        self.assertIn("def play_test_sounds", VOICE_SRC)
        ui = (HERE / "hotkey_ui.py").read_text(encoding="utf-8-sig")
        self.assertIn('label="Play test sounds"', ui)

    def test_test_sounds_command_regex(self):
        rx = r"(ok |okay )?(play |run )?(the )?test sounds?"
        for t in ("test sounds", "play test sounds", "okay run the test sound"):
            self.assertTrue(re.fullmatch(rx, t), t)
        for t in ("test", "sounds", "test the sounds please"):
            self.assertFalse(re.fullmatch(rx, t), t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
