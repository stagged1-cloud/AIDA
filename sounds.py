"""Start/stop/error beeps and the chime, played as small generated WAV files.

Why: winsound.Beep() uses the legacy system-beep path, which stays silent on many Windows 11 setups (headset, USB,
Bluetooth, HDMI default devices). WAV files go through the same winsound.PlaySound(file, ...) path as the AIDA filter
voice, i.e. the normal default output device. No Tk, no numpy: stdlib wave only, so it works under pythonw.

Policy (least surprising):
  * start / stop (mic feedback after a hotkey) always play.
  * chime / error are SKIPPED while AIDA is speaking: PlaySound(SND_ASYNC) would replace the voice's own clip, and
    cutting speech off for a blip is worse than a missing blip. Skipped, not queued: a late chime is just noise.
  * Failures never raise. The first failure per sound is reported once through `log("error", "sound: ...")`.
  * Mute does not touch these (they are not speech). config.yaml `sounds: false` turns them all off.
"""
import math
import pathlib
import struct
import wave

RATE = 22050
PEAK = 0.30                  # of full scale: audible but not loud
EDGE_S = 0.008               # attack / release, avoids clicks

# kind -> [(frequency Hz, duration ms), ...]; same pitches and lengths as the old winsound.Beep calls
SPECS = {
    "start": [(880, 90)],
    "stop": [(600, 90)],
    "error": [(300, 200)],
    "chime": [(988, 60), (1319, 80)],
}
ALWAYS_PLAY = ("start", "stop")          # not skipped while speaking


def tone_samples(freq, ms, rate=RATE, peak=PEAK):
    """One sine tone as 16-bit ints with a linear attack and release."""
    n = max(1, int(rate * ms / 1000))
    edge = max(1, min(int(rate * EDGE_S), n // 2))
    out = []
    for i in range(n):
        env = min(1.0, i / edge, (n - 1 - i) / edge)
        out.append(int(32767 * peak * env * math.sin(2 * math.pi * freq * i / rate)))
    return out


def write_wav(path, parts, rate=RATE):
    """parts = [(freq, ms), ...] back to back, with a 5 ms gap between tones. Returns the path."""
    samples = []
    for k, (freq, ms) in enumerate(parts):
        if k:
            samples += [0] * int(rate * 0.005)
        samples += tone_samples(freq, ms, rate)
    samples += [0] * int(rate * 0.02)        # trailing silence so the device does not clip the tail
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return path


class Sounds:
    """`player` is the winsound module (injectable for tests). `speaking()` says whether AIDA is talking."""

    def __init__(self, folder, enabled=True, speaking=lambda: False, log=None, player=None):
        self.folder = pathlib.Path(folder)
        self.enabled = bool(enabled)
        self.speaking = speaking
        self.log = log or (lambda who, text: None)
        self.player = player
        self.reported = set()
        self.ready = False

    def path(self, kind):
        return self.folder / f"{kind}.wav"

    def ensure(self):
        """Generate any missing or empty WAV once. Idempotent."""
        self.folder.mkdir(parents=True, exist_ok=True)
        for kind, parts in SPECS.items():
            p = self.path(kind)
            if not p.exists() or p.stat().st_size < 100:
                write_wav(p, parts)
        self.ready = True

    def _fail(self, kind, e):
        if kind not in self.reported:                 # once per sound, not every beep
            self.reported.add(kind)
            try:
                self.log("error", f"sound: {kind} failed: {e}")
            except Exception:
                pass

    def play(self, kind, force=False):
        """True if a sound was handed to the player. force = the 'Play test sounds' path: ignores the config switch
        and the speaking check."""
        if kind not in SPECS:
            return False
        if not force:
            if not self.enabled:
                return False
            if kind not in ALWAYS_PLAY and self._speaking():
                return False
        try:
            if not self.ready:
                self.ensure()
            player = self.player
            if player is None:
                import winsound as player
            player.PlaySound(str(self.path(kind)), player.SND_FILENAME | player.SND_ASYNC)
            return True
        except Exception as e:
            self._fail(kind, e)
            return False

    def _speaking(self):
        try:
            return bool(self.speaking())
        except Exception:
            return False
