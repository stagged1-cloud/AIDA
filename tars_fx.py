"""TARS-style processing: slightly lower and slower, speaker-grille band, faint metallic ring."""
import numpy as np
from scipy.signal import butter, sosfilt, resample_poly, lfilter


def tars(audio, rate, pitch=0.94, low=260, high=4200, metal=0.28, metal_ms=2.6, drive=1.4):
    x = audio.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    # lower + slower in one go: play the same samples at a lower rate
    up, down = int(round(pitch * 100)), 100
    x = resample_poly(x, down, up)            # more samples = lower and slower at the same rate
    # speaker-grille band
    sos = butter(4, [low, high], btype="band", fs=rate, output="sos")
    x = sosfilt(sos, x)
    # faint metallic resonance (short feedback comb)
    d = max(1, int(rate * metal_ms / 1000))
    a = np.zeros(d + 1); a[0] = 1.0; a[d] = -metal
    y = lfilter([1.0], a, x)
    # gentle saturation, then normalise
    y = np.tanh(drive * y / (np.max(np.abs(y)) + 1e-9))
    y = y / (np.max(np.abs(y)) + 1e-9) * 0.9
    return y.astype(np.float32)


YESNO = ("want", "do ", "does", "did", "shall", "should", "can", "could", "will", "would", "is ", "are ",
         "was ", "were", "have", "has ", "need", "fancy", "any ", "you ", "ok", "okay", "right", "happy", "ready")


def wants_rise(sentence):
    """Yes/no questions go up at the end; 'what/why/how' questions don't."""
    s = sentence.strip().lower()
    if not s.endswith("?"):
        return False
    s = s.split(",")[-1].strip() if "," in s else s
    return s.startswith(YESNO)


def rise_end(a, rate, semis=4.0, dur=0.5):
    """Reshape the end of a yes/no question so it lifts, using Praat's PSOLA (keeps timing and voice quality).
    The last `dur` seconds of voiced speech glide from where the pitch was to `semis` above it."""
    try:
        import parselmouth
        from parselmouth.praat import call
    except ImportError:
        return a
    a = a.astype(np.float64)
    snd = parselmouth.Sound(a, sampling_frequency=rate)
    pitch = snd.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=300)
    f0 = pitch.selected_array["frequency"]
    times = pitch.xs()
    voiced = np.where(f0 > 0)[0]
    if len(voiced) < 5:
        return a.astype(np.float32)
    t_end = times[voiced[-1]]
    t0 = max(times[voiced[0]], t_end - dur)
    i0 = voiced[np.argmin(np.abs(times[voiced] - t0))]
    f_start = float(np.median(f0[max(0, i0 - 3):i0 + 1][f0[max(0, i0 - 3):i0 + 1] > 0]))
    manip = call(snd, "To Manipulation", 0.01, 60, 300)
    tier = call(manip, "Extract pitch tier")
    call(tier, "Remove points between", t0, snd.duration)
    steps = 8
    for k in range(steps + 1):
        t = t0 + (t_end - t0) * k / steps
        call(tier, "Add point", t, f_start * 2 ** (semis * (k / steps) ** 1.3 / 12))
    call([tier, manip], "Replace pitch tier")
    out = call(manip, "Get resynthesis (overlap-add)")
    y = out.values[0]
    if len(y) < len(a):
        y = np.concatenate([y, np.zeros(len(a) - len(y))])
    return y[: len(a)].astype(np.float32)
