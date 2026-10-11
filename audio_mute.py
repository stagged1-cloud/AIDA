"""Speaker-only Mute: silence this process's Windows audio session while playback carries on in real time.

How it works: Windows Core Audio keeps one (or more) audio SESSION per process on the default output device. Its
ISimpleAudioVolume.SetMute silences that session at the mixer, after the app has handed over its samples. The clip
keeps playing, the playhead keeps moving, and un-muting is audible again from wherever the clip has got to. winsound
and sounddevice (MME) both feed the same process session, so one switch covers the voice, the beeps and the chime.

Safety:
  * Windows remembers a per-app mute in the volume mixer, so a crash while muted could leave the next start silent.
    The muter therefore (a) forces UN-muted at start-up, (b) forces it again at every clip start via reapply(),
    (c) un-mutes at exit (atexit) and (d) never writes anything of its own to disk.
  * A short volume ramp (RAMP_MS) takes the click out of mute/unmute.
  * All COM work happens on one worker thread, so callers (Tk, Mouth, the voice thread) never block or share COM state.
  * Core Audio is injected (backend), so the logic is testable with no audio at all.

No third-party package: the few COM interfaces needed are declared here with comtypes (already in the venv).
"""
import atexit
import os
import threading
import time

RAMP_MS = 12            # total length of the click-removing fade
RAMP_STEPS = 6          # volume steps inside RAMP_MS
POLL_S = 0.05           # while muted, look for brand-new sessions this often (a new clip opens a new session)
BACKLOG_S = 120         # muted this long with replies still queuing: log one line (see Mouth.say)


def ramp_levels(steps=RAMP_STEPS, down=True):
    """Gain steps for a fade, endpoints excluded: down -> 5/6 ... 1/6, up -> 1/6 ... 5/6 (monotonic, strictly in (0,1))."""
    ups = [i / steps for i in range(1, steps)]
    return list(reversed(ups)) if down else ups


# ------------------------------------------------------------------ the real backend (Windows Core Audio via comtypes)
class CoreAudioBackend:
    """Finds this process's sessions on the current default output device, fresh on every call.

    Fresh each time means a changed default device, a Bluetooth reconnect or a re-created session is picked up
    without any notification plumbing. Methods are called only from the muter's worker thread.
    """

    def __init__(self, pid=None):
        self.pid = pid or os.getpid()
        self._ready = False

    def init_thread(self):
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                                    # thread already initialised some other way: fine
        self._declare()
        self._ready = True

    def _declare(self):
        if hasattr(self, "_I"):
            return
        import ctypes
        from ctypes import HRESULT, POINTER, c_float, c_int, c_uint
        from ctypes.wintypes import BOOL, DWORD, LPCWSTR, LPWSTR
        from comtypes import COMMETHOD, GUID, IUnknown

        class ISimpleAudioVolume(IUnknown):
            _iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
            _methods_ = (
                COMMETHOD([], HRESULT, "SetMasterVolume", (["in"], c_float, "fLevel"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(c_float), "pfLevel")),
                COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "bMute"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
            )

        class IAudioMeterInformation(IUnknown):
            _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak")),
            )

        class IAudioSessionControl(IUnknown):
            _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
                COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(LPWSTR), "pRetVal")),
                COMMETHOD([], HRESULT, "SetDisplayName", (["in"], LPCWSTR, "Value"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(LPWSTR), "pRetVal")),
                COMMETHOD([], HRESULT, "SetIconPath", (["in"], LPCWSTR, "Value"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
                COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], POINTER(GUID), "Override"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], ctypes.c_void_p, "n")),
                COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], ctypes.c_void_p, "n")),
            )

        class IAudioSessionControl2(IAudioSessionControl):
            _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(LPWSTR), "pRetVal")),
                COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(LPWSTR), "pRetVal")),
                COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(DWORD), "pRetVal")),
                COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
                COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], BOOL, "optOut")),
            )

        class IAudioSessionEnumerator(IUnknown):
            _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
                COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "SessionCount"),
                          (["out"], POINTER(POINTER(IAudioSessionControl)), "Session")),
            )

        class IAudioSessionManager2(IUnknown):
            _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], POINTER(GUID), "g"), (["in"], DWORD, "f"),
                          (["out"], POINTER(POINTER(IAudioSessionControl)), "o")),
                COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], POINTER(GUID), "g"), (["in"], DWORD, "f"),
                          (["out"], POINTER(POINTER(ISimpleAudioVolume)), "o")),
                COMMETHOD([], HRESULT, "GetSessionEnumerator",
                          (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum")),
                COMMETHOD([], HRESULT, "RegisterSessionNotification", (["in"], ctypes.c_void_p, "n")),
                COMMETHOD([], HRESULT, "UnregisterSessionNotification", (["in"], ctypes.c_void_p, "n")),
                COMMETHOD([], HRESULT, "RegisterDuckNotification", (["in"], LPCWSTR, "id"), (["in"], ctypes.c_void_p, "n")),
                COMMETHOD([], HRESULT, "UnregisterDuckNotification", (["in"], ctypes.c_void_p, "n")),
            )

        class IMMDevice(IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = (
                COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "clsctx"),
                          (["in"], ctypes.c_void_p, "params"), (["out"], POINTER(ctypes.c_void_p), "iface")),
            )

        class IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = (
                COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "flow"), (["in"], DWORD, "mask"),
                          (["out"], POINTER(ctypes.c_void_p), "devs")),
                COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "flow"), (["in"], c_int, "role"),
                          (["out"], POINTER(POINTER(IMMDevice)), "dev")),
            )

        self._I = dict(vol=ISimpleAudioVolume, meter=IAudioMeterInformation, ctl2=IAudioSessionControl2,
                       mgr=IAudioSessionManager2, enum=IMMDeviceEnumerator, dev=IMMDevice)
        self._GUID = GUID
        self._CLSID_ENUM = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

    def _session_controls(self):
        """IAudioSessionControl2 for every session of this process on the default output device."""
        import ctypes
        from comtypes import CLSCTX_ALL, CoCreateInstance
        I = self._I
        enum = CoCreateInstance(self._CLSID_ENUM, I["enum"], CLSCTX_ALL)
        dev = enum.GetDefaultAudioEndpoint(0, 1)                      # eRender, eMultimedia
        raw = dev.Activate(I["mgr"]._iid_, CLSCTX_ALL, None)
        mgr = ctypes.cast(raw, ctypes.POINTER(I["mgr"]))
        se = mgr.GetSessionEnumerator()
        out = []
        for i in range(se.GetCount()):
            ctl = se.GetSession(i)
            c2 = ctl.QueryInterface(I["ctl2"])
            if c2.GetProcessId() == self.pid:
                out.append(c2)
        return out

    def volumes(self):
        """ISimpleAudioVolume objects (SetMute / GetMute / SetMasterVolume / GetMasterVolume) for our sessions."""
        if not self._ready:
            self.init_thread()
        return [c.QueryInterface(self._I["vol"]) for c in self._session_controls()]

    def endpoint_meter(self):
        """Peak meter of the whole default output device (after every app's mute/volume): live check only."""
        import ctypes
        from comtypes import CLSCTX_ALL, CoCreateInstance
        if not self._ready:
            self.init_thread()
        I = self._I
        enum = CoCreateInstance(self._CLSID_ENUM, I["enum"], CLSCTX_ALL)
        dev = enum.GetDefaultAudioEndpoint(0, 1)
        raw = dev.Activate(I["meter"]._iid_, CLSCTX_ALL, None)
        return ctypes.cast(raw, ctypes.POINTER(I["meter"]))

    def meters(self):
        """IAudioMeterInformation objects (GetPeakValue) for our sessions: used only by the live check."""
        if not self._ready:
            self.init_thread()
        return [c.QueryInterface(self._I["meter"]) for c in self._session_controls()]


class _VolWrap:
    """Uniform Python-friendly facade over a comtypes ISimpleAudioVolume (the fake in tests has the same shape)."""

    def __init__(self, v):
        self.v = v

    def set_mute(self, flag):
        self.v.SetMute(1 if flag else 0, None)

    def get_mute(self):
        return bool(self.v.GetMute())

    def set_volume(self, level):
        self.v.SetMasterVolume(float(level), None)

    def get_volume(self):
        return float(self.v.GetMasterVolume())


class RealSessions:
    """Backend adapter: .sessions() -> list of wrapped volume controls. Swapped for a fake in tests."""

    def __init__(self, pid=None):
        self.core = CoreAudioBackend(pid)

    def init_thread(self):
        self.core.init_thread()

    def sessions(self):
        return [_VolWrap(v) for v in self.core.volumes()]


# ------------------------------------------------------------------ the muter
class SessionMuter:
    """Desired-state holder plus a worker thread that makes every session of this process match it.

    set(True)   speaker silent, everything else carries on
    set(False)  audible again at the current playhead
    reapply()   call whenever a new clip starts: new sessions inherit nothing, and a stale persisted mute is cleared
    restore()   force un-muted (start-up, exit, crash recovery)
    """

    def __init__(self, backend=None, log=None, start=True, ramp=True, sleep=time.sleep, poll_s=POLL_S):
        self.backend = backend if backend is not None else RealSessions()
        self.log = log or (lambda who, text: None)
        self.desired = False
        self.ramp = ramp
        self.sleep = sleep
        self.poll_s = poll_s
        self.last_count = None            # sessions found on the last pass (None = never looked)
        self.errors = 0
        self._warned = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="audio-mute", daemon=True)
            self._thread.start()
            self.restore(wait=False)      # a stale per-app mute from a crashed run must never survive start-up

    # ---- public API (any thread)
    def set(self, flag, wait=False):
        self.desired = bool(flag)
        return self._kick(wait)

    def reapply(self, wait=False):
        return self._kick(wait)

    def restore(self, wait=True):
        self.desired = False
        return self._kick(wait)

    def shutdown(self):
        """Exit path: leave the session un-muted whatever happened."""
        self.desired = False
        if self._thread is not None and self._thread.is_alive():
            self._kick(True)
            self._closed = True
            self._wake.set()
        else:
            self.step()

    @property
    def effective(self):
        """False when Mute is on but no session of ours was found (e.g. exclusive-mode device): honest reporting."""
        return not (self.desired and not self.last_count)

    # ---- internals
    def _kick(self, wait):
        if self._thread is None or not self._thread.is_alive():
            return self.step() if not self._closed else 0
        self._idle.clear()
        self._wake.set()
        if wait:
            self._idle.wait(2.0)
        return None

    def _run(self):
        try:
            self.backend.init_thread()
        except Exception as e:
            self._warn("init", e)
        while not self._closed:
            timeout = self.poll_s if self.desired else None
            self._wake.wait(timeout)
            self._wake.clear()
            if self._closed:
                break
            self.step()
            if not self._wake.is_set():
                self._idle.set()
        self._idle.set()

    def step(self):
        """One pass: make every session of this process match `desired`. Returns sessions touched."""
        want = self.desired
        try:
            sessions = self.backend.sessions()
        except Exception as e:
            self._warn("enumerate", e)
            self.last_count = 0
            return 0
        self.last_count = len(sessions)
        for s in sessions:
            try:
                self._match(s, want)
            except Exception as e:
                self._warn("set", e)
        return len(sessions)

    def _match(self, s, want):
        now = s.get_mute()
        if now == want:
            if not want and s.get_volume() < 0.999:
                s.set_volume(1.0)         # a half-finished ramp from a crashed run
            return
        if self.ramp and not want:        # unmuting: start silent, fade up
            s.set_volume(0.0)
            s.set_mute(False)
            for g in ramp_levels(down=False):
                s.set_volume(g)
                self.sleep(RAMP_MS / 1000 / RAMP_STEPS)
            s.set_volume(1.0)
        elif self.ramp and want:          # muting: fade down, then mute, then put the (now silent) volume back
            for g in ramp_levels(down=True):
                s.set_volume(g)
                self.sleep(RAMP_MS / 1000 / RAMP_STEPS)
            s.set_mute(True)
            s.set_volume(1.0)
        else:
            s.set_mute(want)

    def _warn(self, what, e):
        self.errors += 1
        key = (what, type(e).__name__)
        if key not in self._warned:           # one line per kind of failure, not one per 50 ms poll
            self._warned.add(key)
            self.log("voice", f"Mute: audio session {what} failed ({e}).")


def install_exit_restore(muter):
    """Last-chance un-mute on a normal interpreter exit (a hard kill is covered by the start-up restore)."""
    atexit.register(muter.shutdown)
