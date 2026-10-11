"""AIDA 3D floating head: a glossy raymarched robot head with WALL-E style eye acting, drawn on the desktop.

How it works
    * head.frag is a raymarched signed-distance-field head, rendered by ModernGL into an offscreen buffer.
    * The pixels go straight into a Win32 layered window (UpdateLayeredWindow, per-pixel alpha), so the head has
      soft anti-aliased edges and a real glow over whatever is behind it. Transparent pixels and the faint glow
      are click-through; the head itself can be dragged, double-clicked (shrink to a dot) and right-clicked (menu).
    * The GL context lives only in its own low-priority render thread. The Tk main thread only handles mouse
      events and a cheap 40 ms poll. Nothing here touches audio or speech: state comes in through callables.

Emotions (see tmp/face_emotion_spec.md for the design notes)
    Activities from the app:  idle, listening, thinking, speaking  (picked automatically every frame)
    Emotions on top:          happy, sad, shocked, confused, annoyed, sarcastic, smug, curious, sleepy
        face.set_emotion("shocked", intensity=0.9, hold=1.5)    # thread-safe, from any thread
    With get_text supplied, the start of each spoken reply is scanned for simple cues (classify_text) and the head
    picks a matching emotion for as long as that reply is being spoken.

Changing the look
    * Shape, materials, eye geometry, mouth grille: head.frag (comments at the top say where).
    * Poses and timing of every emotion: POSES and TIMING below. Values are deltas the spec describes:
      open 0..1, lid angles in degrees (+ = inner end down = cross), lidBot 0..0.5 (happy squint), head in degrees.
    * Colours: ACTIVITY_COL (state colour) and EMOTION_TINT (mixed in by intensity).
    * Shell colour: face_shell in config.yaml ("white" or "graphite").
"""
import collections
import ctypes
import ctypes.wintypes as wt
import math
import os
import random
import re
import struct
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SHADER_PATH = os.path.join(HERE, "head.frag")

ACTIVITY_COL = {"idle": (0.12, 0.82, 1.0), "listening": (1.0, 0.22, 0.26),
                "thinking": (1.0, 0.60, 0.10), "speaking": (0.22, 1.0, 0.52)}
EMOTION_TINT = {"happy": (1.0, 0.86, 0.30), "sad": (0.30, 0.52, 1.0), "annoyed": (1.0, 0.25, 0.10),
                "shocked": (1.0, 1.0, 1.0), "sarcastic": (0.70, 0.45, 1.0), "smug": (1.0, 0.70, 0.25),
                "confused": (0.80, 0.55, 1.0), "curious": (0.40, 0.95, 1.0), "sleepy": (0.35, 0.45, 0.9)}
SHELLS = {"white": (0.93, 0.94, 0.96), "graphite": (0.16, 0.17, 0.19), "pearl": (0.96, 0.93, 0.90)}

# ------------------------------------------------------------------ rig: neutral pose, poses, timing
NEUTRAL = dict(openL=0.88, openR=0.88, lidL=0.0, lidR=0.0, lidBot=0.0, pupil=1.0, eyeW=1.0, eyeH=1.0, round=0.95,
               spacing=0.0, pitch=0.0, yaw=0.0, roll=0.0, mouthCurve=0.0, mouthTilt=0.0, mouthRest=0.0,
               bobAmp=0.030, bobHz=0.25, lean=1.0, glow=0.80, orbit=0.0, sink=0.0, bright=1.0)
EYE_KEYS = ("openL", "openR", "lidL", "lidR", "lidBot", "pupil", "eyeW", "eyeH", "round", "spacing", "bright")
HEAD_KEYS = ("pitch", "yaw", "roll", "mouthCurve", "mouthTilt", "mouthRest")
BODY_KEYS = ("bobAmp", "bobHz", "lean", "glow", "orbit", "sink")

# Lid angles: + = inner end down (cross / scowl), - = inner end up (sad / worried). Head pitch + = look down,
# roll + = tilt towards screen-right. Values are pushed ~25% past "realistic" so they read at a small size.
POSES = {
    "idle":      dict(),
    "listening": dict(openL=0.98, openR=0.98, lidL=-5, lidR=-5, pupil=1.12, eyeW=1.06, eyeH=1.06, spacing=0.012,
                      pitch=-3, roll=9, bobAmp=0.02, bobHz=0.30, glow=0.95, lean=1.02),
    "thinking":  dict(openL=0.74, openR=0.62, lidL=-4, lidR=12, lidBot=0.10, pupil=0.82, spacing=-0.01,
                      pitch=3, yaw=-11, roll=-7, mouthCurve=-0.2, mouthTilt=0.4, bobAmp=0.02, bobHz=0.40,
                      orbit=1.0, glow=0.85),
    "speaking":  dict(openL=0.90, openR=0.90, bobAmp=0.035, bobHz=0.30, glow=0.85),
    "shocked":   dict(openL=1.0, openR=1.0, lidL=-10, lidR=-10, pupil=0.45, eyeW=1.22, eyeH=1.32, round=1.0,
                      spacing=0.03, pitch=-7, lean=0.95, mouthRest=0.55, bobAmp=0.0, glow=1.15, bright=1.25),
    "happy":     dict(openL=0.86, openR=0.86, lidL=-4, lidR=-4, lidBot=0.42, pupil=1.15, eyeW=1.08,
                      spacing=0.012, pitch=-4, roll=7, mouthCurve=1.0, bobAmp=0.055, bobHz=0.9, glow=1.0),
    "sad":       dict(openL=0.60, openR=0.60, lidL=-24, lidR=-24, pupil=1.12, eyeW=0.94, spacing=-0.008,
                      pitch=11, yaw=-8, roll=-9, mouthCurve=-1.0, bobAmp=0.015, bobHz=0.15, glow=0.55,
                      sink=0.06, bright=0.75),
    "confused":  dict(openL=0.95, openR=0.70, lidL=-8, lidR=12, pupil=0.90, pitch=0, roll=15,
                      mouthCurve=-0.3, mouthTilt=-0.6, bobAmp=0.03),
    "annoyed":   dict(openL=0.52, openR=0.52, lidL=18, lidR=18, lidBot=0.15, pupil=0.80, spacing=-0.015,
                      pitch=4, mouthCurve=-0.6, bobAmp=0.008, bobHz=0.2, glow=0.9),
    "sarcastic": dict(openL=0.62, openR=0.36, lidL=-6, lidR=14, lidBot=0.22, pupil=0.90, spacing=-0.01,
                      pitch=2, roll=5, mouthCurve=0.35, mouthTilt=0.8, bobAmp=0.012, bobHz=0.2),
    "smug":      dict(openL=0.70, openR=0.70, lidL=6, lidR=6, lidBot=0.36, pupil=1.05, eyeW=1.04, spacing=0.01,
                      pitch=-9, roll=-6, mouthCurve=0.9, mouthTilt=0.35, bobAmp=0.03, bobHz=0.2, lean=1.03),
    "curious":   dict(openL=0.98, openR=0.86, lidL=-8, lidR=2, pupil=1.15, eyeW=1.05, eyeH=1.05, spacing=0.01,
                      pitch=-2, roll=11, bobAmp=0.025, bobHz=0.3, glow=0.95),
    "sleepy":    dict(openL=0.28, openR=0.24, lidL=-6, lidR=-6, lidBot=0.10, pupil=1.2, eyeW=0.95,
                      pitch=12, roll=4, bobAmp=0.02, bobHz=0.12, glow=0.4, bright=0.6),
}
# transition time (s) and spring damping (1 = no overshoot, ~0.35 = snappy easeOutBack-like overshoot)
TIMING = {"idle": (0.40, 1.0), "listening": (0.25, 0.6), "thinking": (0.35, 1.0), "speaking": (0.15, 0.9),
          "shocked": (0.09, 0.35), "happy": (0.18, 0.45), "sad": (0.70, 1.0), "confused": (0.20, 0.5),
          "annoyed": (0.16, 1.0), "sarcastic": (0.50, 1.0), "smug": (0.50, 0.75), "curious": (0.25, 0.55),
          "sleepy": (1.20, 1.0)}
EMOTIONS = tuple(k for k in POSES if k not in ("idle", "listening", "thinking", "speaking"))
ACTIVITIES = ("idle", "listening", "thinking", "speaking")
BLINK_RATE = {"listening": 1.6, "thinking": 0.7, "annoyed": 3.0, "sad": 1.1, "sleepy": 0.8, "sarcastic": 1.4}
BLINK_LEN = {"thinking": 0.18, "sad": 0.27, "sleepy": 0.5, "annoyed": 0.2, "sarcastic": 0.24}


def _clamp(x, a, b):
    return a if x < a else b if x > b else x


def _mix3(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


# ------------------------------------------------------------------ reply-text emotion heuristic
_RX = [
    ("shocked",   0.85, r"\b(whoa|woah|blimey|bloody hell|holy (?:shit|hell|crap)|what the (?:hell|fuck)|no way|jesus|christ almighty)\b"),
    ("sad",       0.55, r"\b(sorry|apolog\w*|fail(?:ed|s|ure)?|error\w*|couldn'?t|can'?t|cannot|broke(?:n)?|crash\w*|"
                        r"unfortunately|didn'?t work|not working|lost|missing|blocked|bad news)\b"),
    ("sarcastic", 0.65, r"\b(bloody|fuck\w*|shit\w*|twat|sod|git|dickhead|arse\w*|ass(?:hole)?|idiot|dumbass|bollocks|"
                        r"obviously|congratulations|genius|shocking|bravo|wow,? (?:just )?wow|uprising|lazy)\b"),
    ("happy",     0.55, r"\b(done|worked|works|working now|finished|fixed|sorted|great|nice|brilliant|cheers|passed|"
                        r"all good|success\w*|ready|nailed|perfect|well done|good news|love)\b"),
]
_RX = [(e, k, re.compile(p, re.I)) for e, k, p in _RX]


def classify_text(text):
    """Pick an emotion from simple cues in a reply. Returns (emotion, intensity) or None for neutral.
    Deliberately simple and conservative: first match wins in the order shock, sad, sarcastic, happy, question."""
    t = (text or "").strip()
    if not t or len(t) < 2:
        return None
    head = t[:400]
    for emo, k, rx in _RX:
        if rx.search(head):
            return emo, k
    first = re.split(r"(?<=[.!?])\s", head)
    if any(s.rstrip().endswith("?") for s in first[-2:]) or head.rstrip().endswith("?"):
        return "curious", 0.5
    return None


# ------------------------------------------------------------------ animation engine (pure Python, no GL)
class Animator:
    """Emotion state machine + procedural life (blinks, saccades, float, lip-sync channel). update() returns the
    shader uniforms. Pure Python and deterministic given an RNG, so it can be tested without a window."""

    def __init__(self, rng=None):
        self.r = rng or random.Random()
        # spring per parameter: [x, v, target, omega, zeta]
        self.sp = {k: [v, 0.0, v, 10.0, 1.0] for k, v in NEUTRAL.items()}
        self.sched = []                  # pending pose targets: (time, key, value, omega, zeta)
        self.beats = []                  # scripted one-offs: (time, name, arg)
        self.activity = "idle"
        self.emotion, self.ek, self.e_until, self.e_speech = None, 0.6, None, False
        self.pose = None                 # (name, k) currently applied
        self.pose_t = 0.0
        self.not_speaking_since = None
        self.t_start = None
        # gaze
        self.g = [0.0, 0.0, 0.0, 0.0]            # gx, gy, vx, vy
        self.gt = (0.0, 0.0)
        self.next_gaze = 0.0
        self.glance_back = None
        self.gaze_phase = 0
        self.micro = [0.0, 0.0]
        self.micro_t = (0.0, 0.0)
        self.next_micro = 0.0
        self.follow = [0.0, 0.0, 0.0, 0.0]       # head yaw/pitch following the eyes: x, v (yaw), x, v (pitch)
        self.follow_q = collections.deque()       # delayed follow targets (eyes lead the head)
        self.follow_t = (0.0, 0.0)
        # blinks
        self.blink_t, self.blink_len, self.next_blink = None, 0.2, 0.0
        self.double_pending = None
        self.no_blink_until = 0.0
        # body
        self.phase, self.breath = 0.0, 0.0
        self.hop = [0.0, 0.0]
        self.pod = [0.0, 0.0]
        self.freeze_until = 0.0
        self.wobble_t = None
        self.jitter_until = 0.0
        # voice
        self.env, self.env_lp, self.peak = 0.0, 0.0, 0.05
        self.prev_env, self.last_beat, self.quiet_since, self.pause_blinked = 0.0, 0.0, None, True
        self.dip_until = 0.0
        self.col = ACTIVITY_COL["idle"]
        self.flash = 0.0

    # -- public API
    def set_emotion(self, name, intensity=0.6, hold=None, now=None):
        """Show an emotion. hold: seconds, or "speech" = until the current spoken reply finishes.
        name None / "neutral" clears it."""
        now = time.perf_counter() if now is None else now
        if not name or name in ("neutral", "none") or name in ACTIVITIES:
            self.emotion = None
            return
        if name not in POSES:
            return
        k = _clamp(float(intensity), 0.0, 1.0)
        self.emotion, self.ek = name, k
        self.e_speech = hold == "speech"
        if self.e_speech:
            self.e_until = now + 30.0
        else:
            default = 1.5 if name == "shocked" else 3.5
            self.e_until = now + (float(hold) if hold is not None else default)
        self.pose = None          # force re-entry (re-triggering the same emotion replays its beat)

    # -- springs
    def _target(self, key, val, w, z):
        s = self.sp[key]
        s[2], s[3], s[4] = val, w, z

    def _apply_pose(self, name, k, now):
        tin, zeta = TIMING.get(name, (0.4, 1.0))
        w = 4.2 / tin
        pose = POSES.get(name, {})
        delay = 0.7 if name == "sarcastic" else 0.0     # deadpan: freeze a beat before the line
        self.sched = [e for e in self.sched if e[0] < now - 1]   # drop pending targets of the previous pose
        for key, base in NEUTRAL.items():
            tgt = base + k * (pose.get(key, base) - base)
            cur = self.sp[key][0]
            d0 = delay + (0.0 if key in EYE_KEYS else 0.08 if key in HEAD_KEYS else 0.15)
            if key in ("lidL", "lidR") and cur * tgt < 0 and abs(cur) > 5 and abs(tgt) > 5:
                self.sched.append((now + d0, key, 0.0, 30.0, 1.0))        # pass through neutral, no crossed lids
                d0 += 0.1
            if key in ("pitch", "roll", "yaw") and abs(tgt - cur) > 4:
                self.sched.append((now + d0, key, cur - 0.18 * (tgt - cur), 30.0, 1.0))   # anticipation
                d0 += 0.09
            self.sched.append((now + d0, key, tgt, w, zeta))
        self._enter_beats(name, now)

    def _enter_beats(self, name, now):
        b = self.beats
        if name == "shocked":
            for key in ("eyeW", "eyeH"):          # anticipation: eyes shrink for 60 ms, then pop
                self.sched.insert(0, (now, key, 0.93, 60.0, 1.0))
                self.sched = [e for e in self.sched if not (e[1] == key and e[2] != 0.93)]
                tgt = NEUTRAL[key] + self.ek * (POSES["shocked"][key] - NEUTRAL[key])
                self.sched.append((now + 0.06, key, tgt, 4.2 / 0.09, 0.35))
            self.no_blink_until = now + 0.7
            self.freeze_until = now + 0.15
            self.jitter_until = now + 0.8
            self.flash = 0.7 * self.ek
            b.append((now + 0.7, "blink", 0.08))
            b.append((now + 0.9, "blink", 0.08))
        elif name == "happy":
            self.hop[1] += 0.9 * (0.5 + self.ek)
            b.append((now + 0.22, "hop", 0.7 * (0.5 + self.ek)))
        elif name == "confused":
            self.wobble_t = now + 0.2
            b.append((now + 0.4, "blink", 0.18))
        elif name == "sarcastic":
            self.no_blink_until = now + 1.5
            b.append((now + 1.6, "blink", 0.32))        # long blink on the punchline
        elif name == "annoyed":
            b.append((now + 1.2, "blink", 0.22))
        elif name == "smug":
            b.append((now + 4.0, "blink", 0.22))
        elif name == "thinking":
            b.append((now + 0.05, "blink", 0.26))       # slow blink on entry
        elif name == "listening":
            self.sp["pitch"][1] -= 45.0                 # perk up
            self.sp["openL"][1] += 1.2
            self.sp["openR"][1] += 1.2
        elif name == "sad":
            b.append((now + 0.9, "blink", 0.3))

    def _blink(self, now, length=None):
        if now < self.no_blink_until or self.blink_t is not None:
            return
        self.blink_t = now
        self.blink_len = length or BLINK_LEN.get(self.pose[0] if self.pose else "", 0.0) or self.r.uniform(0.16, 0.24)
        if length is None and self.r.random() < 0.12:
            self.double_pending = now + self.blink_len + self.r.uniform(0.15, 0.25)

    def _blink_factor(self, now):
        if self.blink_t is None:
            return 1.0
        L = self.blink_len
        tc, th = L * 0.33, L * 0.1                   # close fast (ease-in), short hold, open slower (ease-out)
        x = now - self.blink_t
        if x < tc:
            u = x / tc
            return 1.0 - u * u
        if x < tc + th:
            return 0.0
        u = (x - tc - th) / max(1e-3, L - tc - th)
        if u >= 1.0:
            self.blink_t = None
            return 1.0
        return 1.0 - (1.0 - u) ** 3

    # -- gaze behaviour per pose
    def _gaze_tick(self, now, name):
        r = self.r
        if self.glance_back and now >= self.glance_back[0]:
            self._set_gaze(now, *self.glance_back[1])
            self.glance_back = None
            return
        if now < self.next_gaze:
            return
        if name == "thinking":
            self.gaze_phase ^= 1
            side = 1 if self.gaze_phase else -1
            self._set_gaze(now, 0.55 * side + r.uniform(-0.1, 0.1), r.uniform(0.35, 0.55))
            self.next_gaze = now + r.uniform(1.2, 2.0)
        elif name == "listening" or name == "curious":
            self._set_gaze(now, r.uniform(-0.08, 0.08), r.uniform(-0.04, 0.06))
            self.next_gaze = now + r.uniform(2.5, 5.0)
        elif name == "speaking":
            if r.random() < 0.35:
                self._set_gaze(now, r.choice((-1, 1)) * r.uniform(0.35, 0.55), r.uniform(-0.25, 0.2))
                self.glance_back = (now + r.uniform(0.3, 0.5), (r.uniform(-0.06, 0.06), 0.0))
            else:
                self._set_gaze(now, r.uniform(-0.1, 0.1), r.uniform(-0.05, 0.05))
            self.next_gaze = now + r.uniform(3.0, 5.0)
        elif name in ("shocked", "annoyed"):
            self._set_gaze(now, 0.0, 0.0)
            self.next_gaze = now + 1.0
        elif name == "sad":
            self._set_gaze(now, -0.25 + r.uniform(-0.1, 0.1), -0.55 + r.uniform(-0.08, 0.08))
            self.next_gaze = now + r.uniform(2.0, 4.0)
        elif name == "confused":
            self.gaze_phase ^= 1
            self._set_gaze(now, -0.42 if self.gaze_phase else 0.42, 0.12)
            self.next_gaze = now + 0.5
        elif name == "sarcastic":
            if now - self.pose_t < 2.2:
                self._set_gaze(now, 0.0, 0.0)
                self.next_gaze = self.pose_t + 2.2
            else:                                     # slow glance away and back, deliberate
                self._set_gaze(now, 0.62, -0.08, slow=True)
                self.glance_back = (now + 0.7, (0.0, 0.0))
                self.next_gaze = now + r.uniform(4.0, 6.0)
        elif name == "smug":
            self._set_gaze(now, r.uniform(-0.05, 0.05), 0.18)
            self.next_gaze = now + r.uniform(3.0, 5.0)
        elif name == "sleepy":
            self._set_gaze(now, r.uniform(-0.2, 0.2), -0.45)
            self.next_gaze = now + r.uniform(3.0, 6.0)
        elif name == "happy":
            self._set_gaze(now, r.uniform(-0.1, 0.1), r.uniform(0.0, 0.1))
            self.next_gaze = now + r.uniform(2.0, 4.0)
        else:                                          # idle: wander, return to centre now and then
            if r.random() < 0.7:
                self._set_gaze(now, r.uniform(-0.45, 0.45), r.uniform(-0.25, 0.3))
            else:
                self._set_gaze(now, 0.0, 0.0)
            self.next_gaze = now + r.uniform(1.5, 4.0)

    def _set_gaze(self, now, x, y, slow=False):
        jump = math.hypot(x - self.gt[0], y - self.gt[1])
        self.gt = (x, y)
        self.gaze_slow = slow
        self.follow_q.append((now + 0.09, (x * 9.0, -y * 5.0)))     # head follows 90 ms after the eyes
        if jump > 0.35 and self.r.random() < 0.2:
            self._blink(now, 0.17)                                   # blink hides the big saccade

    # -- main update
    def update(self, now, dt, activity="idle", level=0.0):
        if self.t_start is None:
            self.t_start = now
            self.next_blink = now + 1.5
        dt = _clamp(dt, 0.0, 0.1)
        act = activity if activity in ACTIVITIES else "idle"
        if act != self.activity:
            if self.activity == "speaking":
                self.not_speaking_since = now
            if act == "listening" and self.emotion:     # The user is talking again: drop the reply's emotion
                self.emotion = None
            self.activity = act
        if act == "speaking":
            self.not_speaking_since = None
        if self.emotion:
            done = now > self.e_until
            if self.e_speech and self.not_speaking_since and now - self.not_speaking_since > 0.6 and act != "speaking":
                done = True
            if done:
                self.emotion = None
        want = (self.emotion, self.ek) if self.emotion else (act, 1.0)
        if want != self.pose:
            self.pose = want
            self.pose_t = now
            self.next_gaze = now + 0.05
            self.glance_back = None
            self._apply_pose(want[0], want[1], now)
        name = self.pose[0]

        # scheduled targets and beats
        keep = []
        for e in self.sched:
            if e[0] <= now:
                self._target(e[1], e[2], e[3], e[4])
            else:
                keep.append(e)
        self.sched = keep
        keepb = []
        for e in self.beats:
            if e[0] <= now:
                if e[1] == "blink":
                    self.blink_t = None if now >= self.no_blink_until else self.blink_t
                    self._blink(now, e[2])
                elif e[1] == "hop":
                    self.hop[1] += e[2]
            else:
                keepb.append(e)
        self.beats = keepb
        # integrate springs (semi-implicit Euler, substepped)
        n = max(1, int(dt / 0.008) + 1)
        h = dt / n
        for s in self.sp.values():
            for _ in range(n):
                s[1] += (s[3] * s[3] * (s[2] - s[0]) - 2 * s[4] * s[3] * s[1]) * h
                s[0] += s[1] * h
        P = {k: v[0] for k, v in self.sp.items()}

        # voice envelope: gate, auto-scaled peak, fast attack / slow release
        x = max(0.0, float(level or 0.0))
        self.peak = max(x, self.peak * math.exp(-dt / 5.0), 0.04)
        gate = 0.10 * self.peak
        m = _clamp((x - gate) / max(1e-4, self.peak * 0.85 - gate), 0.0, 1.0) ** 0.7
        tau = 0.03 if m > self.env else 0.11
        self.env += (m - self.env) * (1 - math.exp(-dt / tau))
        self.env_lp += (self.env - self.env_lp) * (1 - math.exp(-dt / 0.04))
        denv = (self.env - self.prev_env) / max(dt, 1e-3)
        self.prev_env = self.env
        speaking = act == "speaking"
        if speaking and denv > 6.0 and self.env > 0.4 and now - self.last_beat > 0.2:
            self.last_beat = now
            self.dip_until = now + 0.06
            self.sp["pitch"][1] += 14.0                     # small nod on stressed syllables
        if speaking:
            if self.env < 0.05:
                if self.quiet_since is None:
                    self.quiet_since = now
                elif now - self.quiet_since > 0.25 and not self.pause_blinked:
                    self.pause_blinked = True
                    if self.r.random() < 0.3:
                        self._blink(now)
            else:
                self.quiet_since, self.pause_blinked = None, False

        # gaze: target behaviour, fast saccade spring, micro-saccades, slow drift
        self._gaze_tick(now, name)
        gw, gz = (9.0, 1.0) if getattr(self, "gaze_slow", False) else (38.0, 0.85)
        for i in (0, 1):
            g = self.g
            for _ in range(n):
                g[2 + i] += (gw * gw * (self.gt[i] - g[i]) - 2 * gz * gw * g[2 + i]) * h
                g[i] += g[2 + i] * h
        if now >= self.next_micro:
            calm = name not in ("shocked",) or now - self.pose_t > 0.8
            self.micro_t = (self.r.uniform(-0.03, 0.03), self.r.uniform(-0.012, 0.012)) if calm else (0.0, 0.0)
            self.next_micro = now + self.r.expovariate(2.0) + 0.05
        for i in (0, 1):
            self.micro[i] += (self.micro_t[i] - self.micro[i]) * min(1.0, dt * 40)
        tt = now - self.t_start
        drift = (0.02 * (math.sin(tt * 5.65) + 0.7 * math.sin(tt * 8.2 + 1.0)),
                 0.015 * math.sin(tt * 6.9 + 2.0))
        if name == "thinking":
            drift = (drift[0] + 0.08 * math.sin(tt * 5.03), drift[1])
        gx = _clamp(self.g[0] + self.micro[0] + drift[0], -1, 1)
        gy = _clamp(self.g[1] + self.micro[1] + drift[1], -1, 1)

        # head follows the eyes, late, with a little overshoot
        while self.follow_q and self.follow_q[0][0] <= now:
            self.follow_t = self.follow_q.popleft()[1]
        fw, fz = 9.0, 0.55
        for i in (0, 1):
            f = self.follow
            for _ in range(n):
                f[2 * i + 1] += (fw * fw * (self.follow_t[i] - f[2 * i]) - 2 * fz * fw * f[2 * i + 1]) * h
                f[2 * i] += f[2 * i + 1] * h

        # blinks
        if self.double_pending and now >= self.double_pending:
            self.double_pending = None
            self._blink(now, 0.16)
        if now >= self.next_blink:
            self._blink(now)
            ival = min(9.0, 1.5 + self.r.expovariate(1 / 2.5)) * BLINK_RATE.get(name, 1.0)
            self.next_blink = now + ival
        bf = self._blink_factor(now)

        # body: float, breathing, hops, wobble, jitter
        frozen = now < self.freeze_until
        if not frozen:
            self.phase += 2 * math.pi * P["bobHz"] * dt
            self.breath += 2 * math.pi * dt / 7.5
        bob_amp = max(0.0, P["bobAmp"]) * 1.46 + (0.02 * self.env if speaking else 0.0)
        bob = bob_amp * (math.sin(self.phase) + 0.3 * math.sin(self.phase * 1.48 + 1.0)) / 1.3
        for _ in range(n):
            self.hop[1] += (-16.0 ** 2 * self.hop[0] - 2 * 0.3 * 16.0 * self.hop[1]) * h
            self.hop[0] += self.hop[1] * h
        roll_add = 0.6 * math.sin(tt * 1.07) + (1.5 * math.sin(tt * 1.9) if name == "idle" else 0.0)
        yaw_add = 0.5 * math.sin(tt * 0.7 + 0.4)
        if self.wobble_t is not None:
            u = now - self.wobble_t
            if 0 <= u < 0.4:
                roll_add += 3.0 * math.sin(2 * math.pi * 3.0 * u) * (1 - u / 0.4)
            elif u >= 0.4:
                self.wobble_t = None
        if self.freeze_until < now < self.jitter_until:
            roll_add += 1.2 * math.sin(tt * 6.3) * math.sin(tt * 17.0)
        breath = math.sin(self.breath)
        head_y = 0.10 + bob + self.hop[0] * 0.065 - P["sink"]
        # side pods lag behind the head (secondary motion)
        for _ in range(n):
            self.pod[1] += (12.0 ** 2 * (head_y - self.pod[0]) - 2 * 0.35 * 12.0 * self.pod[1]) * h
            self.pod[0] += self.pod[1] * h
        pod_off = _clamp(self.pod[0] - head_y, -0.05, 0.05)

        # voice-driven mouthless channel
        dip = 0.97 if now < self.dip_until else 1.0
        eopen = 0.05 * self.env if speaking else 0.0
        pitch = P["pitch"] + self.follow[2] + (1.5 * self.env_lp if speaking else 0.0)
        yaw = P["yaw"] + self.follow[0] + yaw_add
        roll = P["roll"] + roll_add
        glow = P["glow"] * (1 + 0.06 * breath)
        if speaking:
            glow = max(glow, 0.7 + 0.35 * self.env)
        scale = (1.0 + 0.012 * breath) * P["lean"]

        # colour: state colour, emotion tint, shock flash
        base = ACTIVITY_COL[act]
        if self.emotion and self.emotion in EMOTION_TINT:
            base = _mix3(base, EMOTION_TINT[self.emotion], 0.45 + 0.4 * self.ek)
        self.flash = max(0.0, self.flash - dt * 1.5)
        base = _mix3(base, (1.0, 1.0, 1.0), self.flash)
        kc = 1 - math.exp(-dt * 6.0)
        self.col = _mix3(self.col, base, kc)

        # mouth grille
        prof = (0.35, 0.62, 0.86, 1.0, 0.86, 0.62, 0.35)
        rest = max(0.0, P["mouthRest"])
        bars = []
        for i in range(7):
            nz = 0.55 + 0.45 * math.sin(tt * (9.0 + 1.7 * i) + i * 1.3) * math.sin(tt * (5.3 + 0.9 * i) + i)
            bars.append(_clamp(rest * prof[i] + self.env * prof[i] * nz * 1.1, 0.0, 1.0))
        mbright = 0.45 + 0.75 * self.env + 0.3 * rest

        lidk = 1.3 * math.pi / 180.0
        squash = 1 + 0.04 * (1 - bf)
        ol = max(0.035, _clamp(P["openL"] + eopen, 0, 1.05) * bf * dip)
        orr = max(0.035, _clamp(P["openR"] + eopen, 0, 1.05) * bf * dip)
        eye_b = (P["eyeW"] * squash, P["eyeH"] * (1 + 0.02 * self.env), _clamp(P["round"], 0.3, 1.0),
                 max(0.2, P["bright"]) * (0.9 + 0.2 * glow))
        float_h = _clamp(0.5 + bob / max(1e-3, 0.06) * 0.5 + self.hop[0] * 2.0, 0.0, 1.0)
        return {
            "u_time": tt,
            "u_rot": (math.radians(yaw), math.radians(pitch), math.radians(roll)),
            "u_off": (0.0, head_y, 0.0),
            "u_scale": scale,
            "u_eyeA": [(ol, P["lidL"] * lidk, _clamp(P["lidBot"], 0, 0.5), max(0.3, P["pupil"])),
                       (orr, P["lidR"] * lidk, _clamp(P["lidBot"], 0, 0.5), max(0.3, P["pupil"]))],
            "u_eyeB": [eye_b, eye_b],
            "u_gaze": (gx, gy),
            "u_spacing": P["spacing"],
            "u_col": self.col,
            "u_glow": _clamp(glow, 0.0, 1.6),
            "u_bars": tuple(bars),
            "u_mouth": (P["mouthCurve"], P["mouthTilt"], mbright),
            "u_orbit": _clamp(P["orbit"], 0.0, 1.0),
            "u_shadow": float_h,
            "u_pod": pod_off,
        }


# ------------------------------------------------------------------ GL renderer (render thread only)
_VS = "#version 330\nin vec2 p; void main(){gl_Position=vec4(p,0,1);}"
_FS_HEAD = "#version 330\nout vec4 fragColor;\n"
_FS_TAIL = "\nvoid main(){ vec4 c = render(gl_FragCoord.xy); fragColor = c.bgra; }\n"


class HeadRenderer:
    def __init__(self, size, shell=(0.93, 0.94, 0.96)):
        import moderngl
        self.mgl = moderngl
        self.size = size
        self.ctx = moderngl.create_standalone_context()
        with open(SHADER_PATH, encoding="utf-8") as f:
            src = f.read()
        self.prog = self.ctx.program(vertex_shader=_VS, fragment_shader=_FS_HEAD + src + _FS_TAIL)
        vbo = self.ctx.buffer(struct.pack("8f", -1, -1, 1, -1, -1, 1, 1, 1))
        self.vao = self.ctx.vertex_array(self.prog, [(vbo, "2f", "p")])
        self.fbo = self.ctx.simple_framebuffer((size, size), components=4)
        self.pbo = [self.ctx.buffer(reserve=size * size * 4) for _ in range(2)]
        self.i = 0
        self.primed = False
        self.info = (self.ctx.info.get("GL_RENDERER", "?"), self.ctx.info.get("GL_VERSION", "?"))
        self._set("u_res", float(size))
        self._set("u_shell", tuple(shell))

    def _set(self, name, value):
        try:
            u = self.prog[name]
        except KeyError:                # optimised away by the driver
            return
        u.value = value

    def draw(self, uni):
        for k, v in uni.items():
            self._set(k, v)
        self.fbo.use()
        self.ctx.clear(0, 0, 0, 0)
        self.vao.render(mode=self.mgl.TRIANGLE_STRIP)
        # async readback through two pixel-pack buffers: this frame queues, last frame is collected
        self.fbo.read_into(self.pbo[self.i], components=4)
        self.i ^= 1
        if not self.primed:
            self.primed = True
            return self.pbo[self.i ^ 1]        # first frame: read the one just queued (waits once)
        return self.pbo[self.i]

    def release(self):
        try:
            self.ctx.release()
        except Exception:
            pass


# ------------------------------------------------------------------ Win32 layered window plumbing
# Own DLL instances: setting argtypes on the shared ctypes.windll objects breaks other libraries
# (uiautomation calls GetAncestor(c_void_p, c_int) -> ArgumentError against our [HWND, UINT]).
u32, g32, k32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32"), ctypes.WinDLL("kernel32")


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


_SIGS = [
    (u32.GetDC, wt.HDC, [wt.HWND]), (u32.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
    (u32.GetWindowLongPtrW, ctypes.c_ssize_t, [wt.HWND, ctypes.c_int]),
    (u32.SetWindowLongPtrW, ctypes.c_ssize_t, [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]),
    (u32.GetAncestor, wt.HWND, [wt.HWND, wt.UINT]),
    (u32.SetWindowPos, wt.BOOL, [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]),
    (u32.GetWindowRect, wt.BOOL, [wt.HWND, ctypes.POINTER(wt.RECT)]),
    (u32.GetCursorPos, wt.BOOL, [ctypes.POINTER(wt.POINT)]),
    (u32.IsWindowVisible, wt.BOOL, [wt.HWND]),
    (g32.CreateCompatibleDC, wt.HDC, [wt.HDC]), (g32.DeleteDC, wt.BOOL, [wt.HDC]),
    (g32.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]), (g32.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
    (g32.CreateDIBSection, wt.HBITMAP, [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                        wt.HANDLE, wt.DWORD]),
    (u32.UpdateLayeredWindow, wt.BOOL, [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                        ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION),
                                        wt.DWORD]),
    (k32.GetCurrentThread, wt.HANDLE, []), (k32.SetThreadPriority, wt.BOOL, [wt.HANDLE, ctypes.c_int]),
]
for _fn, _res, _args in _SIGS:
    _fn.restype, _fn.argtypes = _res, _args

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
ULW_ALPHA = 2
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10


class _DIB:
    def __init__(self, mem_dc, size):
        self.size = size
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        self.hbm = g32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not self.hbm or not bits.value:
            raise OSError("CreateDIBSection failed")
        self.addr = bits.value
        self.n = size * size * 4
        self.view = (ctypes.c_ubyte * self.n).from_address(self.addr)

    def alpha_at(self, x, y):
        if 0 <= x < self.size and 0 <= y < self.size:
            return self.view[(y * self.size + x) * 4 + 3]
        return 0


class LayeredSurface:
    """A DIB per size (full head, dot) and UpdateLayeredWindow. present() is called from the render thread."""

    def __init__(self, hwnd, sizes, opacity=1.0):
        self.hwnd = hwnd
        self.screen_dc = u32.GetDC(None)
        self.mem_dc = g32.CreateCompatibleDC(self.screen_dc)
        self.dibs = {s: _DIB(self.mem_dc, s) for s in sizes}
        self.blend = BLENDFUNCTION(0, 0, int(_clamp(opacity, 0.2, 1.0) * 255), 1)    # AC_SRC_OVER, AC_SRC_ALPHA
        self.src = wt.POINT(0, 0)
        self.current = None

    def present(self, size, pos=None):
        dib = self.dibs[size]
        g32.SelectObject(self.mem_dc, dib.hbm)
        pt = wt.POINT(*pos) if pos else None
        sz = wt.SIZE(size, size)
        ok = u32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(pt) if pt else None, ctypes.byref(sz),
                                     self.mem_dc, ctypes.byref(self.src), 0, ctypes.byref(self.blend), ULW_ALPHA)
        self.current = dib
        return ok

    def close(self):
        try:
            for d in self.dibs.values():
                g32.DeleteObject(d.hbm)
            g32.DeleteDC(self.mem_dc)
            u32.ReleaseDC(None, self.screen_dc)
        except Exception:
            pass


def work_area():
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    try:
        r = RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)    # SPI_GETWORKAREA
        if r.r > r.l and r.b > r.t:
            return r.l, r.t, r.r, r.b
    except Exception:
        pass
    return None


def virtual_screen():
    gm = ctypes.windll.user32.GetSystemMetrics
    x, y, w, h = gm(76), gm(77), gm(78), gm(79)
    return (x, y, x + w, y + h) if w > 0 and h > 0 else None


# ------------------------------------------------------------------ the floating head (Tk host)
class Face3D:
    """Drop-in for face.Face: same constructor shape, .win, .dead, toggle_dot(), reset_pos(), plus
    set_emotion(name, intensity, hold). Raises if the GPU path cannot start, so callers can fall back."""

    def __init__(self, root, scale, get_state, get_level, size=260, pos=None, opacity=1.0, on_moved=None,
                 log=None, fps=30, get_text=None, shell="white", auto_emotion=True, topmost=True):
        import tkinter as tk
        self.tk, self.root, self.s = tk, root, scale
        self.get_state, self.get_level, self.get_text = get_state, get_level, get_text
        self.on_moved = on_moved
        self.log = log or (lambda *a: None)
        self.size = max(120, int(size * scale)) // 2 * 2
        self.dot_size = max(24, int(30 * scale))
        self.fps = _clamp(int(fps or 30), 10, 60)
        self.auto_emotion = bool(auto_emotion)
        self.shell = SHELLS.get(str(shell).lower(), SHELLS["white"]) if not isinstance(shell, (tuple, list)) else tuple(shell)
        self.anim = Animator()
        self.dead = False
        self.dot = False
        self._run = True
        self._req = collections.deque()
        self._drag = None
        self._dragging = False
        self._click_through = None
        self.frame_ms = collections.deque(maxlen=900)
        self.gl_info = None
        self.err = None

        w = self.win = tk.Toplevel(root)
        w.withdraw()
        w.overrideredirect(True)
        if topmost:
            w.attributes("-topmost", True)
        w.configure(bg="black")
        self.x, self.y = self._clamp_pos(*self._start_pos(pos))
        w.geometry(f"{self.size}x{self.size}+{self.x}+{self.y}")
        w.update_idletasks()
        self.hwnd = u32.GetAncestor(w.winfo_id(), 2) or w.winfo_id()     # GA_ROOT: the real top-level HWND
        ex = u32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        u32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.surface = LayeredSurface(self.hwnd, (self.size, self.dot_size), opacity)
        self._mode = None                  # what the render thread last presented: "full" / "dot"

        # the GL context is created and used only in the render thread; wait for it to come up (or fail)
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="face3d-render", daemon=True)
        self.thread.start()
        if not self._ready.wait(10.0) or self.err is not None:
            self._run = False
            err = self.err or RuntimeError("3D face render thread did not start in time")
            self.surface.close()
            try:
                w.destroy()
            except Exception:
                pass
            raise RuntimeError(f"3D face unavailable: {err}")

        w.deiconify()
        for seq, fn in (("<ButtonPress-1>", self._grab), ("<B1-Motion>", self._move),
                        ("<ButtonRelease-1>", self._drop), ("<Double-Button-1>", lambda e: self.toggle_dot()),
                        ("<Button-3>", self._menu)):
            w.bind(seq, fn)
        m = self.menu = tk.Menu(w, tearoff=0)
        m.add_command(label="Shrink to dot / restore", command=self.toggle_dot)
        m.add_command(label="Reset position", command=self.reset_pos)
        em = tk.Menu(m, tearoff=0)
        for e in EMOTIONS:
            em.add_command(label=e.capitalize(), command=lambda e=e: self.set_emotion(e, 1.0, 3.0))
        em.add_separator()
        em.add_command(label="Show them all", command=self.show_off)
        m.add_cascade(label="Emotions", menu=em)
        self.log("face", f"3D face running on {self.gl_info[0]} ({self.size}px, {self.fps} fps)")
        self._poll()

    # -- public
    def set_emotion(self, name, intensity=0.6, hold=None):
        """Thread-safe: queue an emotion for the render thread."""
        self._req.append((name, intensity, hold))

    def show_off(self, gap=2.6):
        for i, e in enumerate(EMOTIONS):
            self.root.after(int(i * gap * 1000), lambda e=e: self.set_emotion(e, 1.0, gap - 0.2))

    def stop(self):
        self._run = False

    def stats(self):
        ms = sorted(self.frame_ms)
        if not ms:
            return None
        return dict(frames=len(ms), mean=sum(ms) / len(ms), p50=ms[len(ms) // 2], p95=ms[int(len(ms) * 0.95)],
                    max=ms[-1])

    # -- positions
    def _start_pos(self, pos):
        if pos and len(pos) == 2:
            return int(pos[0]), int(pos[1])
        return self.default_pos()

    def default_pos(self):
        wa = work_area() or (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        m = int(8 * self.s)
        return wa[2] - self.size - m, wa[3] - self.size - m

    def _clamp_pos(self, x, y):
        vs = virtual_screen()
        if not vs:
            return x, y
        # keep at least the middle of the head on screen (the window has a transparent margin)
        lo = -self.size // 4
        x = int(_clamp(x, vs[0] + lo, vs[2] - self.size - lo))
        y = int(_clamp(y, vs[1] + lo, vs[3] - self.size - lo))
        return x, y

    def _dot_pos(self):
        off = (self.size - self.dot_size) // 2
        return self.x + off, self.y + off

    def _window_xy(self):
        r = wt.RECT()
        u32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def _move_window(self, x, y):
        u32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def reset_pos(self):
        self.x, self.y = self.default_pos()
        self._move_window(*(self._dot_pos() if self.dot else (self.x, self.y)))
        if self.on_moved:
            self.on_moved(None, None)

    def toggle_dot(self):
        self.dot = not self.dot          # the render thread resizes the window on its next frame

    # -- mouse (Tk thread)
    def _grab(self, e):
        l, t, _, _ = self._window_xy()
        self._drag = (e.x_root - l, e.y_root - t)
        self._dragging = False

    def _move(self, e):
        if not self._drag:
            return
        self._dragging = True
        nx, ny = e.x_root - self._drag[0], e.y_root - self._drag[1]
        self._move_window(nx, ny)
        if self.dot:
            off = (self.size - self.dot_size) // 2
            self.x, self.y = nx - off, ny - off
        else:
            self.x, self.y = nx, ny

    def _drop(self, e):
        if self._drag and self._dragging and self.on_moved:
            try:
                self.on_moved(self.x, self.y)
            except Exception:
                pass
        self._drag = None
        self._dragging = False

    def _menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _set_click_through(self, on):
        if on == self._click_through:
            return
        ex = u32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
        u32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        self._click_through = on

    def _poll(self):
        """Tk thread, every 40 ms: glow and empty pixels pass clicks through; only the solid head catches them."""
        if self.dead or not self._run:
            self._teardown()
            return
        try:
            if not self._drag:
                p = wt.POINT()
                u32.GetCursorPos(ctypes.byref(p))
                l, t, r, b = self._window_xy()
                if l <= p.x < r and t <= p.y < b and self.surface.current is not None:
                    a = self.surface.current.alpha_at(p.x - l, p.y - t)
                    self._set_click_through(a < 96)
        except Exception:
            pass
        self.root.after(40, self._poll)

    def _teardown(self):
        self._run = False
        if getattr(self, "_torn", False):
            return
        self._torn = True
        try:
            self.win.destroy()
        except Exception:
            pass

    # -- render thread
    def _loop(self):
        r = None
        try:
            try:
                k32.SetThreadPriority(k32.GetCurrentThread(), -1)       # THREAD_PRIORITY_BELOW_NORMAL
            except Exception:
                pass
            r = HeadRenderer(self.size, self.shell)
            self.gl_info = r.info
            now = time.perf_counter()
            pbo = r.draw(self.anim.update(now, 0.016, "idle", 0.0))
            pbo.read_into(self.surface.dibs[self.size].view)
        except Exception as e:
            self.err = e
            self._ready.set()
            if r:
                r.release()
            return
        self._ready.set()
        last_tt = None
        if self.get_text:
            try:
                last_tt = self.get_text()[1]
            except Exception:
                last_tt = None
        period = 1.0 / self.fps
        last = time.perf_counter()
        failures = 0
        try:
            while self._run:
                f0 = time.perf_counter()
                dt, last = f0 - last, f0
                if self.dot:
                    self._draw_dot(f0)
                    time.sleep(1 / 12)
                    continue
                if self._mode != "full":
                    self._mode = "full"
                    self.surface.present(self.size, (self.x, self.y))
                activity, level = "idle", 0.0
                try:
                    activity = self.get_state() or "idle"
                    level = float(self.get_level() or 0.0)
                except Exception:
                    pass
                if self.get_text and self.auto_emotion:
                    try:
                        txt, tt = self.get_text()
                        if tt != last_tt:
                            last_tt = tt
                            emo = classify_text(txt)
                            if emo:
                                self.anim.set_emotion(emo[0], emo[1], hold="speech", now=f0)
                    except Exception:
                        pass
                while self._req:
                    name, k, hold = self._req.popleft()
                    self.anim.set_emotion(name, k, hold, now=f0)
                uni = self.anim.update(f0, dt, activity, level)
                pbo = r.draw(uni)
                pbo.read_into(self.surface.dibs[self.size].view)
                if not self.surface.present(self.size):
                    failures += 1                     # e.g. session locked; keep going quietly
                else:
                    failures = 0
                spent = time.perf_counter() - f0
                self.frame_ms.append(spent * 1000)
                time.sleep(max(0.002, period - spent) if failures < 30 else 0.5)
        except Exception as e:
            self.err = e
            self.dead = True
            try:
                self.log("face", f"3D face stopped: {e}")
            except Exception:
                pass
        finally:
            r.release()
            try:
                self.surface.close()
            except Exception:
                pass

    def _draw_dot(self, now):
        """Shrunk mode: a small glowing orb in the state colour, drawn on the CPU (GL rendering is paused)."""
        if self._mode != "dot":
            self._mode = "dot"
            self.surface.present(self.dot_size, self._dot_pos())
        try:
            act = self.get_state() or "idle"
        except Exception:
            act = "idle"
        col = ACTIVITY_COL.get(act, ACTIVITY_COL["idle"])
        d = self.dot_size
        dib = self.surface.dibs[d]
        pulse = 0.5 + 0.5 * math.sin(now * 3.0)
        c = (d - 1) / 2.0
        R = d / 2.0 - 1.0
        buf = bytearray(d * d * 4)
        for y in range(d):
            for x in range(d):
                rr = math.hypot(x - c, y - c) / R
                if rr >= 1.0:
                    continue
                core = max(0.0, 1.0 - rr / 0.45)
                body = 1.0 if rr < 0.78 else max(0.0, (1.0 - rr) / 0.22)
                a = body
                lum = 0.18 + 0.82 * core * (0.75 + 0.25 * pulse) + 0.35 * max(0.0, 1 - abs(rr - 0.7) / 0.08)
                rgb = [min(1.0, ch * lum + 0.3 * core * core) * a for ch in col]
                i = (y * d + x) * 4
                buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = int(rgb[2] * 255), int(rgb[1] * 255), int(rgb[0] * 255), int(a * 255)
        ctypes.memmove(dib.addr, bytes(buf), len(buf))
        self.surface.present(d)
