"""OBS Studio remote control for AIDA via obs-websocket v5 (built into OBS 28+).

No Tk, no UI. Stdlib only (minimal RFC 6455 client). Every public method of
ObsRecorder NEVER raises and returns a dict with 'ok' (bool) and 'error' (str,
empty on success). The password is never printed or logged.

Protocol reference: github.com/obsproject/obs-websocket docs/generated/protocol.md
(Hello op0, Identify op1, Identified op2, Request op6, RequestResponse op7).
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

SCENE_NAME = "AIDA Screen"
INPUT_NAME = "AIDA Display"
RPC_VERSION = 1
DEFAULT_TIMEOUT = 4.0
EVENTS_OUTPUTS = 1 << 6          # eventSubscription bit for Outputs (RecordStateChanged)
START_VERIFY_SECS = 5.0          # StartRecord only means "request accepted": poll until the output is really active
STOP_SETTLE_SECS = 4.0
MAX_ENCODE_WIDTH = 4096          # NVENC / H.264 hard limit on width; a wider two-screen canvas is scaled down to this
VIDEO_BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "obs_video_backup.json")
# NOT under Documents/Pictures/Videos/Desktop: Windows Defender Controlled Folder Access blocks obs64.exe from writing
# there ("Unable to open file" in the OBS log, 'Failed to start recording' dialog), which is why the default is the
# user-profile root.
DEFAULT_RECORD_DIR = os.path.join(os.path.expanduser("~"), "AIDA Recordings")
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
# (key, OBS input name, OBS input kind, spoken label). Kinds verified against OBS 31 with GetInputKindList; default
# settings from GetInputDefaultSettings: {'device_id': 'default'} for both.
AUDIO_SOURCES = [("desktop", "AIDA Desktop Audio", "wasapi_output_capture", "PC sound"),
                 ("mic", "AIDA Mic", "wasapi_input_capture", "microphone")]
AUDIO_LABELS = {k: lbl for k, _, _, lbl in AUDIO_SOURCES}
RECORD_TRACK = "1"                  # the recording uses audio track 1 (profile RecTracks=1)
OBS_LAUNCH_FLAGS =["--minimize-to-tray", "--disable-shutdown-check"]   # real OBS flags
_MON_ITEM = re.compile(r"(\d+)\s*x\s*(\d+)\s*@\s*(-?\d+)\s*,\s*(-?\d+)")


class ObsError(Exception):
    """Internal. kind: refused | auth | protocol | timeout | request | other."""

    def __init__(self, kind, msg, code=0):
        Exception.__init__(self, msg)
        self.kind = kind
        self.msg = msg
        self.code = code


# --------------------------------------------------------------------------
# Auth (obs-websocket v5): secret = b64(sha256(password+salt));
# auth = b64(sha256(secret+challenge))
# --------------------------------------------------------------------------
def make_auth(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


# --------------------------------------------------------------------------
# Minimal websocket client
# --------------------------------------------------------------------------
class _WS:
    def __init__(self, host, port, timeout):
        self.timeout = timeout
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except socket.timeout:
            # Windows can take seconds to report a closed port: treat as unreachable
            raise ObsError("refused", "Connection timed out")
        except ConnectionRefusedError:
            raise ObsError("refused", "Connection refused")
        except OSError as e:
            raise ObsError("refused", "Cannot connect: %s" % e.__class__.__name__)
        self.sock.settimeout(timeout)
        self.buf = b""
        try:
            self._handshake(host, port)
        except Exception:
            self.close()
            raise

    def _read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise ObsError("timeout", "Timed out waiting for OBS")
            except OSError as e:
                raise ObsError("protocol", "Connection error: %s" % e.__class__.__name__)
            if not chunk:
                raise ObsError("protocol", "Connection closed by OBS")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
               "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n" % (host, port, key))
        try:
            self.sock.sendall(req.encode("ascii"))
        except OSError as e:
            raise ObsError("protocol", "Send failed: %s" % e.__class__.__name__)
        while b"\r\n\r\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise ObsError("timeout", "Timed out during websocket handshake")
            except OSError as e:
                raise ObsError("protocol", "Handshake error: %s" % e.__class__.__name__)
            if not chunk:
                raise ObsError("protocol", "Server closed during handshake (not an OBS WebSocket server?)")
            self.buf += chunk
            if len(self.buf) > 65536:
                raise ObsError("protocol", "Handshake response too large")
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        text = head.decode("latin-1")
        if " 101 " not in text.split("\r\n")[0]:
            raise ObsError("protocol", "Server is not a websocket (unexpected HTTP reply)")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept.lower() not in text.lower():
            raise ObsError("protocol", "Websocket handshake accept key mismatch")

    def send_text(self, s):
        payload = s.encode("utf-8")
        hdr = bytearray([0x81])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(bytes(hdr) + masked)
        except OSError as e:
            raise ObsError("protocol", "Send failed: %s" % e.__class__.__name__)

    def recv_text(self):
        """Return next text message. Raises ObsError('close', ..., code) on close frame."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 16 * 1024 * 1024:
                raise ObsError("protocol", "Frame too large")
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
                raise ObsError("close", "Closed by OBS (code %d)" % code, code)
            if op == 9:  # ping -> pong
                try:
                    self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                except OSError:
                    pass
                continue
            if op == 10:
                continue
            parts.append(data)
            if fin:
                try:
                    return b"".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    raise ObsError("protocol", "Non-UTF8 message")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class _Session:
    """One connected + identified obs-websocket session."""

    def __init__(self, host, port, password, timeout, events=0):
        self.ws = _WS(host, port, timeout)
        self.events = []          # events seen while waiting for request responses (only if subscribed)
        self.event_mask = events
        try:
            self._identify(password)
        except Exception:
            self.ws.close()
            raise

    def _recv_json(self):
        raw = self.ws.recv_text()
        try:
            msg = json.loads(raw)
        except ValueError:
            raise ObsError("protocol", "Malformed JSON from OBS")
        if not isinstance(msg, dict) or not isinstance(msg.get("d"), dict) or "op" not in msg:
            raise ObsError("protocol", "Malformed message from OBS")
        return msg

    def _identify(self, password):
        try:
            hello = self._recv_json()
        except ObsError as e:
            if e.kind == "close":
                raise ObsError("protocol", e.msg)
            raise
        if hello["op"] != 0:
            raise ObsError("protocol", "Expected Hello from OBS")
        d = {"rpcVersion": RPC_VERSION, "eventSubscriptions": self.event_mask}
        auth = hello["d"].get("authentication")
        if auth:
            if not password:
                raise ObsError("auth", "OBS WebSocket needs a password (set obs_password in config.yaml)")
            if not isinstance(auth, dict) or "salt" not in auth or "challenge" not in auth:
                raise ObsError("protocol", "Malformed authentication block from OBS")
            d["authentication"] = make_auth(password, str(auth["salt"]), str(auth["challenge"]))
        self.ws.send_text(json.dumps({"op": 1, "d": d}))
        try:
            ident = self._recv_json()
        except ObsError as e:
            if e.kind == "close" and e.code == 4009:
                raise ObsError("auth", "Wrong OBS WebSocket password (check obs_password in config.yaml)")
            if e.kind == "close":
                raise ObsError("protocol", e.msg)
            raise
        if ident["op"] != 2:
            raise ObsError("protocol", "Expected Identified from OBS")

    def request(self, rtype, data=None):
        """Return responseData dict; raises ObsError('request') on failure status."""
        rid = uuid.uuid4().hex
        d = {"requestType": rtype, "requestId": rid}
        if data:
            d["requestData"] = data
        self.ws.send_text(json.dumps({"op": 6, "d": d}))
        while True:
            try:
                msg = self._recv_json()
            except ObsError as e:
                if e.kind == "close":
                    raise ObsError("protocol", e.msg)
                raise
            if msg["op"] == 5:
                self.events.append(msg["d"])
            if msg["op"] != 7 or msg["d"].get("requestId") != rid:
                continue  # events or stray messages
            st = msg["d"].get("requestStatus")
            if not isinstance(st, dict) or "result" not in st:
                raise ObsError("protocol", "Malformed RequestResponse")
            if not st["result"]:
                raise ObsError("request", "%s failed: %s" % (rtype, st.get("comment") or "code %s" % st.get("code")),
                               int(st.get("code") or 0) if isinstance(st.get("code"), (int, float)) else 0)
            rd = msg["d"].get("responseData")
            return rd if isinstance(rd, dict) else {}

    def close(self):
        self.ws.close()


# --------------------------------------------------------------------------
# Windows helpers
# --------------------------------------------------------------------------
def find_obs():
    """Path of obs64.exe, or ''."""
    try:
        cands = []
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "obs-studio", "bin", "64bit", "obs64.exe"))
        if sys.platform == "win32":
            try:
                import winreg
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    for sub in (r"SOFTWARE\OBS Studio", r"SOFTWARE\WOW6432Node\OBS Studio"):
                        try:
                            with winreg.OpenKey(hive, sub) as k:
                                val = winreg.QueryValueEx(k, "")[0]
                            cands.append(os.path.join(val, "bin", "64bit", "obs64.exe"))
                        except OSError:
                            pass
            except Exception:
                pass
        cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "obs-studio", "bin", "64bit", "obs64.exe"))
        cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "obs-studio-portable", "bin", "64bit", "obs64.exe"))  # install_obs.ps1 -Portable
        for c in cands:
            if c and os.path.isfile(c):
                return c
    except Exception:
        pass
    return ""


def find_ffmpeg():
    """ffmpeg.exe on PATH or under the winget Gyan.FFmpeg package, else ''."""
    import shutil, glob
    p = shutil.which("ffmpeg")
    if p:
        return p
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    hits = sorted(glob.glob(os.path.join(base, "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe")))
    return hits[-1] if hits else ""


_VOL_RE = re.compile(r"(mean|max)_volume:\s*(-?inf|-?[\d.]+) dB")


def parse_volumedetect(text):
    """ffmpeg volumedetect stderr -> {'has_audio', 'mean_db', 'max_db', 'silent'}. -inf/no match = no usable audio.
    'silent' = peak below -60 dB (digital silence or a dead source)."""
    vals = {}
    for kind, v in _VOL_RE.findall(text or ""):
        vals[kind] = float("-inf") if "inf" in v else float(v)
    has = "max" in vals
    mx = vals.get("max", float("-inf"))
    return {"has_audio": has, "mean_db": vals.get("mean", float("-inf")), "max_db": mx,
            "silent": (not has) or mx < -60.0}


def check_audio(path, ffmpeg=None, timeout=60):
    """Measure a finished recording's sound. Returns parse_volumedetect()'s dict plus 'checked' (False when ffmpeg is
    missing or fails, so the caller can say it couldn't verify rather than guess)."""
    exe = ffmpeg if ffmpeg is not None else find_ffmpeg()
    if not exe or not os.path.isfile(path):
        return {"checked": False, "has_audio": False, "mean_db": float("-inf"), "max_db": float("-inf"), "silent": True}
    try:
        cp = subprocess.run([exe, "-hide_banner", "-nostats", "-i", path, "-map", "0:a?", "-af", "volumedetect",
                             "-f", "null", "-"], capture_output=True, text=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {"checked": False, "has_audio": False, "mean_db": float("-inf"), "max_db": float("-inf"), "silent": True}
    out = parse_volumedetect(cp.stderr)
    out["checked"] = True
    return out


def win_monitors():
    """[{'index','name','rect':[l,t,r,b]}] via EnumDisplayMonitors; [] on failure."""
    if sys.platform != "win32":
        return []
    try:
        from ctypes import wintypes

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]
        out = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(hmon, hdc, lprc, lparam):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                name = mi.szDevice.replace("\\\\.\\", "")
                if mi.dwFlags & 1:
                    name += " (primary)"
                out.append({"index": len(out), "name": name, "rect": [r.left, r.top, r.right, r.bottom]})
            return 1
        ctypes.windll.user32.EnumDisplayMonitors(None, None, proto(cb), 0)
        return out
    except Exception:
        return []


def _obs_running_process():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True,
                           text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "obs64.exe" in r.stdout.lower()
    except Exception:
        return False


def hide_obs_windows(pid, timeout=25.0, interval=0.1):
    """Hide (SW_HIDE, no activation) every visible top-level OBS main window owned by `pid` until `timeout` passes.
    Only used on an OBS process WE launched; never on one that was already running. ctypes only. Blocking."""
    if sys.platform != "win32" or not pid:
        return 0
    hidden = 0
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = []

            def cb(hwnd, lparam):
                try:
                    p = wintypes.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
                        n = user32.GetWindowTextLengthW(hwnd)
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(hwnd, buf, n + 1)
                        if buf.value.startswith("OBS"):          # main window 'OBS 31.0.2 - Profile: ...'
                            found.append(hwnd)
                except Exception:
                    pass
                return True
            user32.EnumWindows(proto(cb), 0)
            for h in found:
                user32.ShowWindow(h, 0)                           # SW_HIDE: gone from screen and taskbar, tray icon stays
                hidden += 1
            time.sleep(interval)
    except Exception:
        pass
    return hidden


def parse_monitor_item(name):
    """'S32E590C: 1920x1080 @ 1920,0' -> [l, t, r, b] or None."""
    m = _MON_ITEM.search(str(name or ""))
    if not m:
        return None
    w, h, x, y = (int(g) for g in m.groups())
    return [x, y, x + w, y + h]


def match_item_rect(name, win_mons):
    """Last-resort rect for an OBS monitor item whose name has no geometry: the one Windows monitor whose size appears
    in the name (only if exactly one does). Never guesses by list position."""
    m = re.search(r"(\d+)\s*x\s*(\d+)", str(name or ""))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    hits = [x["rect"] for x in win_mons or [] if x["rect"][2] - x["rect"][0] == w and x["rect"][3] - x["rect"][1] == h]
    return list(hits[0]) if len(hits) == 1 else None


def obs_index_for_rect(items, rect):
    """Index of the OBS monitor item whose reported geometry ('WxH @ x,y') equals rect [l,t,r,b]. Raises ObsError
    ('other') with a plain message if none matches. Order of `items` is irrelevant."""
    for i, it in enumerate(items or []):
        if parse_monitor_item(it.get("itemName") if isinstance(it, dict) else it) == [int(v) for v in rect]:
            return i
    raise ObsError("other", "OBS has no screen at %s (its screens: %s)" % (
        list(rect), "; ".join(str(it.get("itemName")) for it in items or [] if isinstance(it, dict)) or "none"))


def parse_device_items(items):
    """OBS property items -> [{'id','name'}], enabled ones only, 'Default' (id 'default') first, no duplicates."""
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("itemEnabled") is False:
            continue
        val, nm = it.get("itemValue"), it.get("itemName")
        if not isinstance(val, str) or not val or val in seen:
            continue
        seen.add(val)
        out.append({"id": val, "name": str(nm or val)})
    if "default" not in seen:
        out.insert(0, {"id": "default", "name": "Default"})
    else:
        out.sort(key=lambda d: d["id"] != "default")
    return out


# Folders Windows Defender Controlled Folder Access protects by default: OBS gets "Unable to open file" in them.
_PROTECTED = ("Desktop", "Documents", "Pictures", "Videos", "Music")


def folder_warning(path, home=None):
    """'' if `path` looks fine, else a plain warning. Never writes anything. Checks: protected profile folder (incl. under
    a OneDrive copy), then that the nearest existing parent is a writable directory."""
    p = str(path or "").strip()
    if not p:
        return ""
    home = os.path.normcase(os.path.abspath(home or os.path.expanduser("~")))
    norm = os.path.normcase(os.path.abspath(p))
    roots = [os.path.join(home, n) for n in _PROTECTED] + [os.path.join(home, "OneDrive", n) for n in _PROTECTED]
    for r in roots:
        r = os.path.normcase(r)
        if norm == r or norm.startswith(r + os.sep):
            return ("Windows may block OBS writing here; pick another folder or allow obs64.exe in Windows Security.")
    probe = norm
    while probe and not os.path.exists(probe):
        up = os.path.dirname(probe)
        if up == probe:
            break
        probe = up
    if not os.path.isdir(probe) or not os.access(probe, os.W_OK):
        return "That folder is not writable; pick another folder."
    return ""


def number_monitors(mons):
    """Add 'number' (1-based, left to right by rect.left; ties by index), 'side', 'label' (fully formed text such as
    'Screen 1 (left) 1920x1080') and 'size' to each monitor dict. Returns a new list in the original order."""
    mons = [dict(m) for m in mons if isinstance(m, dict) and "index" in m]
    rects = all(isinstance(m.get("rect"), (list, tuple)) and len(m["rect"]) == 4 for m in mons)
    order = sorted(mons, key=(lambda m: (m["rect"][0], m["index"])) if rects else (lambda m: m["index"]))
    sides = {2: ["left", "right"], 3: ["left", "middle", "right"]}.get(len(order))
    for i, m in enumerate(order):
        m["number"] = i + 1
        m["side"] = sides[i] if sides else ""
        m["size"] = "%dx%d" % (m["rect"][2] - m["rect"][0], m["rect"][3] - m["rect"][1]) if rects else ""
        m["label"] = ("Screen %d" % (i + 1) + (" (%s)" % m["side"] if m["side"] else "") +
                      (" " + m["size"] if m["size"] else ""))
    return mons


def _obs_log_hint():
    """Best-effort: the reason OBS logged for the last failed output, from the newest OBS log. '' if unknown."""
    try:
        d = os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "logs")
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".txt")), key=os.path.getmtime)
        if not files:
            return ""
        with open(files[-1], encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-400:]
        for ln in reversed(lines):
            m = re.search(r"Unable to open file '([^']+)'", ln)
            if m:
                folder = os.path.dirname(m.group(1).replace("/", os.sep))
                return ("OBS was blocked from writing to %s (Unable to open file). Windows Controlled Folder "
                        "Access does this to Videos, Documents, Pictures and Desktop. Record to another folder "
                        "(obs_record_dir in config.yaml) or allow obs64.exe in Windows Security." % folder)
            if re.search(r"nvenc|encoder.*(fail|error)|Failed to open (NVENC|encoder)", ln, re.I) and \
                    re.search(r"fail|error|unable", ln, re.I):
                return "OBS's video encoder failed to start: " + ln.split(": ", 1)[-1][:120]
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
class ObsRecorder:
    def __init__(self, host="localhost", port=4455, password="", timeout=DEFAULT_TIMEOUT, record_dir=None,
                 verify_secs=START_VERIFY_SECS, poll=0.25, audio_desktop=True, audio_mic=False, state_dir=None,
                 mic_device=""):
        self.audio_desktop, self.audio_mic = bool(audio_desktop), bool(audio_mic)    # config defaults
        self.mic_device = str(mic_device or "")
        self.state_dir = STATE_DIR if state_dir is None else str(state_dir)
        self.host = host or "localhost"
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = 4455
        self._password = password or ""
        self.timeout = timeout
        self.record_dir = DEFAULT_RECORD_DIR if record_dir is None else str(record_dir)   # '' = leave OBS's own folder
        self.verify_secs, self.poll = verify_secs, poll

    def __repr__(self):  # never expose the password
        return "ObsRecorder(host=%r, port=%r)" % (self.host, self.port)

    # -- plumbing --
    def _open(self, events=0):
        return _Session(self.host, self.port, self._password, self.timeout, events)

    @staticmethod
    def _fail(e, **extra):
        if isinstance(e, ObsError):
            if e.kind == "refused":
                msg = ("Cannot reach OBS WebSocket. OBS may not be running, or the WebSocket server is "
                       "disabled (OBS > Tools > WebSocket Server Settings > Enable).")
            else:
                msg = e.msg
        else:
            msg = "Unexpected error: %s" % e.__class__.__name__
        r = {"ok": False, "error": msg}
        r.update(extra)
        return r

    # -- public API --
    def status(self):
        base = {"connected": False, "obs_running": False, "recording": False, "seconds": 0.0}
        s = None
        try:
            s = self._open()
            base["connected"] = True
            base["obs_running"] = True
            rd = s.request("GetRecordStatus")
            base["recording"] = bool(rd.get("outputActive", False))
            try:
                base["seconds"] = float(rd.get("outputDuration", 0) or 0) / 1000.0  # ms
            except (TypeError, ValueError):
                base["seconds"] = 0.0
            base.update({"ok": True, "error": ""})
            return base
        except Exception as e:
            r = self._fail(e)
            base.update(r)
            if isinstance(e, ObsError) and e.kind in ("auth", "close", "request", "protocol", "timeout"):
                base["obs_running"] = True if e.kind in ("auth", "request") else base["obs_running"]
            return base
        finally:
            if s:
                s.close()

    def ensure_ready(self):
        try:
            if not find_obs():
                return {"ok": False, "error": "OBS Studio is not installed (obs64.exe not found)."}
            s = None
            try:
                s = self._open()
                s.request("GetVersion")
                return {"ok": True, "error": ""}
            finally:
                if s:
                    s.close()
        except Exception as e:
            r = self._fail(e)
            if isinstance(e, ObsError) and e.kind == "refused":
                r["error"] = ("OBS is not running." if not _obs_running_process() else
                              "OBS is running but its WebSocket server is off: enable it in "
                              "Tools > WebSocket Server Settings.")
            return {"ok": False, "error": r["error"]}

    def list_monitors(self):
        """{'ok','error','monitors':[{index,name,rect,number,side,size,label}]}. index = OBS monitor index; number is
        1-based left to right; label is the full display text ('Screen 1 (left) 1920x1080')."""
        try:
            mons = win_monitors()
            s = None
            try:
                s = self._open()
                items = self._obs_monitor_items(s, create=True)
            except Exception:
                items = []
            finally:
                if s:
                    s.close()
            if items:
                out = []
                for i, it in enumerate(items):
                    name = str(it.get("itemName", "Monitor %d" % i))
                    rect = parse_monitor_item(name)
                    if rect is None:
                        m = re.search(r"@\s*(-?\d+)\s*,\s*(-?\d+)", name)
                        if m:
                            x, y = int(m.group(1)), int(m.group(2))
                            for w in mons:
                                if w["rect"][0] == x and w["rect"][1] == y:
                                    rect = w["rect"]
                    if rect is None:
                        rect = match_item_rect(name, mons)
                    ent = {"index": i, "name": name}
                    if rect:
                        ent["rect"] = list(rect)
                    out.append(ent)
                return {"ok": True, "error": "", "monitors": number_monitors(out)}
            if mons:
                return {"ok": True, "error": "", "monitors": number_monitors(mons)}
            return {"ok": False, "error": "Could not enumerate monitors", "monitors": []}
        except Exception as e:
            return self._fail(e, monitors=[])

    # -- record --
    @staticmethod
    def _norm_indexes(monitor_index, monitor_indexes):
        raw = list(monitor_indexes) if monitor_indexes is not None and not isinstance(monitor_indexes, (int, str)) \
            else ([monitor_indexes] if monitor_indexes is not None else [])
        if not raw and monitor_index is not None:
            raw = [monitor_index]
        out = []
        for v in raw:
            iv = int(v)                      # ValueError -> reported by the caller
            if iv not in out:
                out.append(iv)
        return out

    def _set_record_dir(self, s, folder=None):
        folder = self.record_dir if folder is None else folder
        if not folder:
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        try:
            s.request("SetRecordDirectory", {"recordDirectory": folder})
        except ObsError as e:
            if e.kind != "request":          # older OBS without SetRecordDirectory: carry on with its own folder
                raise

    def _wait_started(self, s):
        """StartRecord only means the request was accepted. Poll until the output is really active, or fail with
        the real reason (RecordStateChanged event, else the OBS log)."""
        deadline = time.time() + self.verify_secs
        while True:
            for ev in s.events:
                if ev.get("eventType") == "RecordStateChanged":
                    d = ev.get("eventData") or {}
                    if d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED":
                        return False, _obs_log_hint() or "OBS stopped the recording as soon as it started."
            s.events[:] = []
            rd = s.request("GetRecordStatus")
            if rd.get("outputActive"):
                return True, ""
            if time.time() >= deadline:
                return False, (_obs_log_hint() or
                               "OBS accepted the request but did not start recording within %d seconds."
                               % int(self.verify_secs))
            time.sleep(self.poll)

    # -- audio choice: remembered between recordings in state/obs_audio.json (config.yaml obs_audio_* are the defaults)
    @property
    def _prefs_path(self):
        return os.path.join(self.state_dir, "obs_audio.json")

    @property
    def _audio_backup_path(self):
        return os.path.join(self.state_dir, "obs_audio_backup.json")

    def _load_saved(self):
        try:
            with open(self._prefs_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            return saved if isinstance(saved, dict) else {}
        except Exception:
            return {}

    def prefs(self):
        """Everything the record dialog remembers: desktop, mic, audio (master), record_dir, mic_device.
        Last saved choice wins, else the config defaults. Never raises."""
        saved = self._load_saved()
        out = {"desktop": self.audio_desktop, "mic": self.audio_mic, "audio": True,
               "record_dir": self.record_dir, "mic_device": self.mic_device}
        for k in ("desktop", "mic", "audio"):
            if isinstance(saved.get(k), bool):
                out[k] = saved[k]
        if isinstance(saved.get("record_dir"), str) and saved["record_dir"].strip():     # blank = not chosen
            out["record_dir"] = saved["record_dir"]
        if isinstance(saved.get("mic_device"), str):
            out["mic_device"] = saved["mic_device"]
        return out

    def audio_prefs(self):
        """{'desktop': bool, 'mic': bool}: the last recording's choice, else the config defaults."""
        p = self.prefs()
        return {"desktop": p["desktop"], "mic": p["mic"]}

    def save_prefs(self, **kw):
        """Merge desktop / mic / audio / record_dir / mic_device into the saved file."""
        try:
            saved = self._load_saved()
            for k, v in kw.items():
                if k in ("desktop", "mic", "audio"):
                    saved[k] = bool(v)
                elif k == "record_dir" and v:
                    saved[k] = str(v)
                elif k == "mic_device" and v is not None:
                    saved[k] = str(v)
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self._prefs_path, "w", encoding="utf-8") as fh:
                json.dump(saved, fh)
        except OSError:
            pass

    def save_audio_prefs(self, desktop, mic):
        self.save_prefs(desktop=desktop, mic=mic)

    def list_mic_devices(self):
        """{'ok','error','devices':[{'id','name'}]} from OBS itself (device_id property of a wasapi_input_capture input).
        Reads an existing input where possible; only creates 'AIDA Mic' if OBS has no mic input at all. 'Default' is
        always first; if OBS cannot list, devices is just [Default] and ok is False."""
        default = {"id": "default", "name": "Default"}
        s = None
        try:
            s = self._open()
            inputs = [i for i in s.request("GetInputList").get("inputs", []) if isinstance(i, dict)]
            names = [i.get("inputName") for i in inputs]
            cand = "AIDA Mic" if "AIDA Mic" in names else next(
                (i.get("inputName") for i in inputs if i.get("inputKind") == "wasapi_input_capture"), None)
            if cand is None:
                self._ensure_audio_input(s, "AIDA Mic", "wasapi_input_capture", set(names))
                cand = "AIDA Mic"
            items = s.request("GetInputPropertiesListPropertyItems",
                              {"inputName": cand, "propertyName": "device_id"}).get("propertyItems", [])
            return {"ok": True, "error": "", "devices": parse_device_items(items) or [default]}
        except Exception as e:
            return self._fail(e, devices=[default])
        finally:
            if s:
                s.close()

    def _ensure_audio_input(self, s, name, kind, existing):
        scenes = s.request("GetSceneList").get("scenes", [])
        if not any(isinstance(x, dict) and x.get("sceneName") == SCENE_NAME for x in scenes):
            s.request("CreateScene", {"sceneName": SCENE_NAME})
        if name not in existing:
            s.request("CreateInput", {"sceneName": SCENE_NAME, "inputName": name, "inputKind": kind,
                                      "inputSettings": {"device_id": "default"}, "sceneItemEnabled": True})
            existing.add(name)
            return
        items = s.request("GetSceneItemList", {"sceneName": SCENE_NAME}).get("sceneItems", [])
        if not any(isinstance(i, dict) and i.get("sourceName") == name for i in items):
            s.request("CreateSceneItem", {"sceneName": SCENE_NAME, "sourceName": name})

    def _setup_audio(self, s, want_desktop, want_mic, mic_device=""):
        """Make the AIDA audio inputs match the choice: wanted = unmuted on track 1, unwanted = muted (never deleted).
        OBS's own global Desktop Audio / Mic inputs are muted for the recording (and restored after) so the choice is
        honest. A source OBS refuses becomes a warning: the video still records. Returns (active_keys, warnings)."""
        active, warns = [], []
        wants = {"desktop": want_desktop, "mic": want_mic}
        try:
            self._restore_audio_globals(s)                 # leftovers from a recording stopped outside AIDA
            kinds = s.request("GetInputKindList", {"unversioned": True}).get("inputKinds", [])
            existing = {i.get("inputName") for i in s.request("GetInputList").get("inputs", []) if isinstance(i, dict)}
        except ObsError as e:
            if e.kind != "request":
                raise
            return [], ["I couldn't read OBS's audio sources (%s)." % e.msg]
        for key, name, kind, label in AUDIO_SOURCES:
            want = wants[key]
            try:
                if not want and name not in existing:
                    continue
                if kind not in kinds:
                    raise ObsError("other", "this OBS has no %s source" % kind)
                if want:
                    self._ensure_audio_input(s, name, kind, existing)
                    if key == "mic":
                        s.request("SetInputSettings", {"inputName": name, "overlay": True,
                                                       "inputSettings": {"device_id": mic_device or "default"}})
                    tracks = {str(n): (str(n) == RECORD_TRACK) for n in range(1, 7)}
                    s.request("SetInputAudioTracks", {"inputName": name, "inputAudioTracks": tracks})
                s.request("SetInputMute", {"inputName": name, "inputMuted": not want})
                if want:
                    active.append(key)
            except ObsError as e:
                if e.kind not in ("request", "other"):
                    raise
                if want:
                    warns.append("OBS wouldn't add the %s (%s), so it is not in the recording." % (label, e.msg))
        try:
            self._mute_audio_globals(s)
        except ObsError as e:
            if e.kind != "request":
                raise
        return active, warns

    def _mute_audio_globals(self, s):
        """Mute OBS's global Desktop Audio / Mic inputs for this recording, remembering how they were."""
        specials = s.request("GetSpecialInputs")
        prior = {}
        for nm in specials.values():
            if not nm or nm in [n for _, n, _, _ in AUDIO_SOURCES]:
                continue
            try:
                prior[nm] = bool(s.request("GetInputMute", {"inputName": nm}).get("inputMuted"))
                s.request("SetInputMute", {"inputName": nm, "inputMuted": True})
            except ObsError as e:
                if e.kind != "request":
                    raise
                prior.pop(nm, None)
        if prior:
            try:
                os.makedirs(self.state_dir, exist_ok=True)
                with open(self._audio_backup_path, "w", encoding="utf-8") as fh:
                    json.dump(prior, fh)
            except OSError:
                pass

    def _restore_audio_globals(self, s):
        """Put the global audio inputs back the way they were. Silent best effort."""
        try:
            if not os.path.isfile(self._audio_backup_path):
                return
            with open(self._audio_backup_path, encoding="utf-8") as fh:
                prior = json.load(fh)
            for nm, muted in prior.items():
                try:
                    s.request("SetInputMute", {"inputName": nm, "inputMuted": bool(muted)})
                except ObsError as e:
                    if e.kind != "request":
                        raise
            os.remove(self._audio_backup_path)
        except ObsError:
            raise
        except Exception:
            pass

    def start(self, monitor_index=None, monitor_indexes=None, audio_desktop=None, audio_mic=None, record_dir=None,
              mic_device=None, remember=True, monitor_rects=None):
        """Start recording one screen or several. monitor_rects ([l,t,r,b] each) selects screens by geometry. Several screens go into ONE file on a wide canvas (OBS cannot run two
        record outputs at once). audio_desktop / audio_mic: True/False, or None = the remembered choice (else config).
        Returns {ok, error, path, paths, audio, warnings}; ok only once OBS confirms it is recording. `audio` lists the
        active sources ('desktop', 'mic'); an audio source OBS refuses is a warning, the video still records."""
        s = None
        try:
            try:
                idxs = self._norm_indexes(monitor_index, monitor_indexes)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Bad monitor index", "path": "", "paths": [], "audio": [], "warnings": []}
            if monitor_rects:                # rect [l,t,r,b] wins over any index: matched to OBS's own geometry
                s0 = self._open()
                try:
                    items = self._obs_monitor_items(s0, create=True)
                finally:
                    s0.close()
                idxs = []
                for rc in monitor_rects:
                    k = obs_index_for_rect(items, rc)
                    if k not in idxs:
                        idxs.append(k)
            pref = self.prefs()
            want_d = (pref["desktop"] and pref["audio"]) if audio_desktop is None else bool(audio_desktop)
            want_m = (pref["mic"] and pref["audio"]) if audio_mic is None else bool(audio_mic)
            folder = pref["record_dir"] if record_dir is None else str(record_dir)
            device = pref["mic_device"] if mic_device is None else str(mic_device)
            s = self._open(events=EVENTS_OUTPUTS)
            if s.request("GetRecordStatus").get("outputActive"):
                return {"ok": False, "error": "OBS is already recording", "path": "", "paths": [], "audio": [],
                        "warnings": []}
            self._set_record_dir(s, folder)
            if idxs:
                self._setup_monitors(s, idxs)
            audio, warnings = self._setup_audio(s, want_d, want_m, device)
            try:
                s.request("StartRecord")
            except ObsError as e:
                if e.kind == "request" and e.code == 500:
                    return {"ok": False, "error": "OBS is already recording", "path": "", "paths": [], "audio": [],
                            "warnings": []}
                raise
            ok, why = self._wait_started(s)
            if not ok:
                self._restore_video(s)
                self._restore_audio_globals(s)
                return {"ok": False, "error": why, "path": "", "paths": [], "audio": [], "warnings": []}
            if remember:
                keep = {}                      # only what was explicitly chosen; a plain start changes nothing
                if audio_desktop is not None:
                    keep["desktop"] = want_d
                if audio_mic is not None:
                    keep["mic"] = want_m
                if want_d or want_m:
                    keep["audio"] = True
                self.save_prefs(record_dir=folder, mic_device=device, **keep)
            return {"ok": True, "error": "", "path": "", "paths": [], "audio": audio, "warnings": warnings}
        except Exception as e:
            return self._fail(e, path="", paths=[], audio=[], warnings=[])
        finally:
            if s:
                s.close()

    def stop(self):
        s = None
        try:
            s = self._open()
            try:
                rd = s.request("StopRecord")
            except ObsError as e:
                if e.kind == "request" and e.code == 501:
                    try:
                        self._restore_audio_globals(s)      # a recording stopped inside OBS may have left them muted
                    except Exception:
                        pass
                    return {"ok": False, "error": "OBS is not recording", "path": "", "paths": []}
                raise
            p = rd.get("outputPath", "")
            p = p if isinstance(p, str) else ""
            # StopRecord returns before the file is finalised: wait until OBS says the output is inactive
            end = time.time() + STOP_SETTLE_SECS
            while time.time() < end:
                try:
                    if not s.request("GetRecordStatus").get("outputActive"):
                        break
                except ObsError:
                    break
                time.sleep(self.poll)
            self._restore_video(s)
            try:
                self._restore_audio_globals(s)
            except Exception:
                pass
            return {"ok": True, "error": "", "path": p, "paths": [p] if p else []}
        except Exception as e:
            return self._fail(e, path="", paths=[])
        finally:
            if s:
                s.close()

    def launch_obs(self, hide=True):
        """Start OBS hidden (tray) if not running. Returns {ok, error, launched}. A window that appears anyway is hidden
        by hide_obs_windows on a daemon thread. An OBS that was already running is left completely alone."""
        try:
            if _obs_running_process():
                return {"ok": True, "error": "", "launched": False}
            exe = find_obs()
            if not exe:
                return {"ok": False, "error": "OBS Studio is not installed", "launched": False}
            # --minimize-to-tray and --disable-shutdown-check are real OBS flags (no --startreplaybuffer etc.).
            kw = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()          # SW_SHOWMINNOACTIVE: minimised, doesn't take focus
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 7
                kw["startupinfo"] = si
            p = subprocess.Popen([exe] + OBS_LAUNCH_FLAGS,
                                 cwd=os.path.dirname(exe), close_fds=True,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), **kw)
            pid = getattr(p, "pid", None)
            if hide and pid:
                threading.Thread(target=hide_obs_windows, args=(pid,), daemon=True).start()
            return {"ok": True, "error": "", "launched": True}
        except Exception as e:
            return {"ok": False, "error": "Could not launch OBS: %s" % e.__class__.__name__, "launched": False}

    # -- monitor selection --
    @staticmethod
    def _kind(s):
        kinds = s.request("GetInputKindList", {"unversioned": True}).get("inputKinds", [])
        if "monitor_capture" in kinds:
            return "monitor_capture"
        if "display_capture" in kinds:
            return "display_capture"
        raise ObsError("other", "OBS has no display capture source kind")

    @staticmethod
    def _input_name(k):
        return INPUT_NAME if k == 0 else "%s %d" % (INPUT_NAME, k + 1)

    def _obs_monitor_items(self, s, create=False):
        """Property items of the 'AIDA Display' monitor-capture input. With create=True the AIDA scene/input are made
        if missing (the property list can only be read from an existing input); else [] when it doesn't exist."""
        inputs = s.request("GetInputList").get("inputs", [])
        if not any(isinstance(i, dict) and i.get("inputName") == INPUT_NAME for i in inputs):
            if not create:
                return []
            self._ensure_input(s, self._kind(s), INPUT_NAME, 0, set_program=False)
        for prop in ("monitor_id", "monitor"):
            try:
                its = s.request("GetInputPropertiesListPropertyItems",
                                {"inputName": INPUT_NAME, "propertyName": prop}).get("propertyItems", [])
                if isinstance(its, list) and its:
                    return [i for i in its if isinstance(i, dict)]
            except ObsError:
                continue
        return []

    def _ensure_input(self, s, kind, name, idx, set_program=True):
        """Create scene/input/scene-item as needed and point the input at OBS monitor `idx`.
        Returns the monitor's property item name (e.g. 'DELL: 1920x1080 @ 0,0') or ''."""
        scenes = s.request("GetSceneList").get("scenes", [])
        if not any(isinstance(x, dict) and x.get("sceneName") == SCENE_NAME for x in scenes):
            s.request("CreateScene", {"sceneName": SCENE_NAME})
        inputs = s.request("GetInputList").get("inputs", [])
        exists = any(isinstance(i, dict) and i.get("inputName") == name for i in inputs)
        if not exists:
            s.request("CreateInput", {"sceneName": SCENE_NAME, "inputName": name,
                                      "inputKind": kind, "inputSettings": {"monitor": idx},
                                      "sceneItemEnabled": True})
        else:
            items = s.request("GetSceneItemList", {"sceneName": SCENE_NAME}).get("sceneItems", [])
            if not any(isinstance(i, dict) and i.get("sourceName") == name for i in items):
                s.request("CreateSceneItem", {"sceneName": SCENE_NAME, "sourceName": name})
        settings, label = {"monitor": idx}, ""
        try:
            its = s.request("GetInputPropertiesListPropertyItems",
                            {"inputName": name, "propertyName": "monitor_id"}).get("propertyItems", [])
            if isinstance(its, list) and 0 <= idx < len(its) and isinstance(its[idx], dict) \
                    and its[idx].get("itemValue") is not None:
                settings["monitor_id"] = its[idx]["itemValue"]
                label = str(its[idx].get("itemName", ""))
            elif isinstance(its, list) and its and not (0 <= idx < len(its)):
                raise ObsError("other", "Monitor %d not available in OBS (%d found)" % (idx, len(its)))
        except ObsError as e:
            if e.kind != "request":
                raise
        s.request("SetInputSettings", {"inputName": name, "inputSettings": settings, "overlay": True})
        if set_program:
            s.request("SetCurrentProgramScene", {"sceneName": SCENE_NAME})
        return label

    def _select_monitor(self, s, idx):
        self._setup_monitors(s, [idx])

    def _setup_monitors(self, s, idxs):
        kind = self._kind(s)
        labels = [self._ensure_input(s, kind, self._input_name(k), idx) for k, idx in enumerate(idxs)]
        if len(idxs) == 1:
            self._restore_video(s)
            self._layout(s, [(0, self._input_name(0), None)], len(idxs))
            return
        # Two or more screens: ONE canvas wide enough for all of them, side by side, left to right by position.
        rects = [parse_monitor_item(l) for l in labels]
        if any(r is None for r in rects):
            raise ObsError("other", "OBS did not report the size of every selected screen")
        order = sorted(range(len(idxs)), key=lambda k: (rects[k][0], idxs[k]))
        x, placed = 0, []
        for k in order:
            w = rects[k][2] - rects[k][0]
            placed.append((x, self._input_name(k), (rects[k][3] - rects[k][1])))
            x += w
        width, height = x, max(h for _, _, h in placed)
        self._set_canvas(s, width, height)
        self._layout(s, [(px, nm, None) for px, nm, _ in placed], len(idxs))

    def _layout(self, s, placed, used):
        """Enable the used AIDA inputs at their x offsets, disable stale extra AIDA inputs. Best effort."""
        try:
            items = s.request("GetSceneItemList", {"sceneName": SCENE_NAME}).get("sceneItems", [])
        except ObsError:
            return
        want = {nm: px for px, nm, _ in placed}
        for it in items:
            if not isinstance(it, dict) or not str(it.get("sourceName", "")).startswith(INPUT_NAME):
                continue
            iid, nm = it.get("sceneItemId"), it.get("sourceName")
            if iid is None:
                continue
            try:
                s.request("SetSceneItemEnabled", {"sceneName": SCENE_NAME, "sceneItemId": iid,
                                                  "sceneItemEnabled": nm in want})
                if nm in want:
                    s.request("SetSceneItemTransform", {"sceneName": SCENE_NAME, "sceneItemId": iid,
                                                        "sceneItemTransform": {
                                                            "positionX": float(want[nm]), "positionY": 0.0,
                                                            "alignment": 5, "boundsType": "OBS_BOUNDS_NONE",
                                                            "scaleX": 1.0, "scaleY": 1.0,
                                                            "cropLeft": 0, "cropRight": 0, "cropTop": 0,
                                                            "cropBottom": 0}})
            except ObsError:
                continue

    # -- canvas (video settings) for the multi-screen case; the original is saved and restored after the recording --
    def _set_canvas(self, s, width, height):
        cur = s.request("GetVideoSettings")
        if not os.path.isfile(VIDEO_BACKUP):          # never overwrite the first (real) backup
            try:
                os.makedirs(os.path.dirname(VIDEO_BACKUP), exist_ok=True)
                with open(VIDEO_BACKUP, "w", encoding="utf-8") as fh:
                    json.dump(cur, fh)
            except OSError:
                pass
        ow, oh = width, height
        if ow > MAX_ENCODE_WIDTH:
            oh = int(round(height * MAX_ENCODE_WIDTH / float(width)))
            ow = MAX_ENCODE_WIDTH
        ow, oh = ow - ow % 2, oh - oh % 2
        want = {"baseWidth": int(width), "baseHeight": int(height), "outputWidth": int(ow), "outputHeight": int(oh)}
        if all(cur.get(k) == v for k, v in want.items()):
            return
        for k in ("fpsNumerator", "fpsDenominator"):
            if k in cur:
                want[k] = cur[k]
        try:
            s.request("SetVideoSettings", want)
        except ObsError as e:
            raise ObsError("other", "OBS would not switch to a wide canvas for both screens: %s" % e.msg)

    def _restore_video(self, s):
        """Put the saved video settings back (after a two-screen recording). Silent best effort."""
        try:
            if not os.path.isfile(VIDEO_BACKUP):
                return
            with open(VIDEO_BACKUP, encoding="utf-8") as fh:
                old = json.load(fh)
            keep = {k: old[k] for k in ("baseWidth", "baseHeight", "outputWidth", "outputHeight",
                                        "fpsNumerator", "fpsDenominator") if k in old}
            s.request("SetVideoSettings", keep)
            os.remove(VIDEO_BACKUP)
        except Exception:
            pass


def from_config(cfg):
    """Build an ObsRecorder from the config dict (obs_host, obs_port, obs_password, obs_record_dir)."""
    try:
        cfg = cfg or {}
        rd = cfg.get("obs_record_dir")
        return ObsRecorder(host=cfg.get("obs_host") or "localhost",
                           port=cfg.get("obs_port") or 4455,
                           password=str(cfg.get("obs_password") or ""),
                           record_dir=str(rd) if rd else None,
                           audio_desktop=bool(cfg.get("obs_audio_desktop", True)),
                           audio_mic=bool(cfg.get("obs_audio_mic", False)),
                           mic_device=str(cfg.get("obs_mic_device") or ""))
    except Exception:
        return ObsRecorder()
