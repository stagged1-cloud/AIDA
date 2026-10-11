"""Tests for obs_control using a fake in-process obs-websocket v5 server."""
import base64
import hashlib
import json
import socket
import struct
import threading
import unittest

import obs_control
from obs_control import ObsRecorder, make_auth

import os
import tempfile

# The recorder remembers its audio / folder choice in state/obs_audio.json: tests must never touch the real one.
obs_control.STATE_DIR = tempfile.mkdtemp(prefix="obs_test_state_")

def reset_state():
    for f in os.listdir(obs_control.STATE_DIR):
        os.remove(os.path.join(obs_control.STATE_DIR, f))


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(c, n):
    b = b""
    while len(b) < n:
        ch = c.recv(n - len(b))
        if not ch:
            raise ConnectionError
        b += ch
    return b


def _read_frame(c):
    b1, b2 = _recv_exact(c, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(c, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(c, 8))[0]
    mask = _recv_exact(c, 4) if b2 & 0x80 else None
    d = _recv_exact(c, n)
    if mask:
        d = bytes(b ^ mask[i % 4] for i, b in enumerate(d))
    return b1 & 0x0F, d


def _frame(op, payload):
    n = len(payload)
    h = bytes([0x80 | op])
    if n < 126:
        h += bytes([n])
    elif n < 65536:
        h += bytes([126]) + struct.pack(">H", n)
    else:
        h += bytes([127]) + struct.pack(">Q", n)
    return h + payload


class FakeObs:
    """Speaks Hello/Identify/Identified/RequestResponse. Behaviour knobs are plain attributes."""

    def __init__(self, password="", mode="normal"):
        self.password = password
        self.mode = mode  # normal | notws | silent | badjson | nohello | closeafterhello
        self.responses = {}   # requestType -> (result, code, comment, data)
        self.requests = []    # (type, data)
        self.state = {"recording": False, "scenes": [], "inputs": [], "items": [], "stuck_start": False,
                      "video": {"baseWidth": 1920, "baseHeight": 1080, "outputWidth": 1920, "outputHeight": 1080,
                                "fpsNumerator": 60, "fpsDenominator": 1}, "transforms": {}, "enabled": {}}
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.salt, self.challenge = "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=", "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except Exception:
            pass

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _send(self, c, obj):
        c.sendall(_frame(1, json.dumps(obj).encode()))

    def _serve(self, c):
        try:
            c.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                data += c.recv(4096)
            if self.mode == "notws":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                return
            key = [l.split(":", 1)[1].strip() for l in data.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            if self.mode == "silent":
                import time
                time.sleep(3)
                return
            if self.mode == "nohello":
                self._send(c, {"op": 7, "d": {}})
                return
            hello = {"obsStudioVersion": "30", "obsWebSocketVersion": "5.5.2", "rpcVersion": 1}
            if self.password:
                hello["authentication"] = {"challenge": self.challenge, "salt": self.salt}
            self._send(c, {"op": 0, "d": hello})
            if self.mode == "closeafterhello":
                return
            op, d = _read_frame(c)
            ident = json.loads(d)["d"]
            if self.password:
                if ident.get("authentication") != make_auth(self.password, self.salt, self.challenge):
                    c.sendall(_frame(8, struct.pack(">H", 4009) + b"auth failed"))
                    return
            self._send(c, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
            while True:
                op, d = _read_frame(c)
                if op == 8:
                    return
                msg = json.loads(d)["d"]
                t, rid, rdata = msg["requestType"], msg["requestId"], msg.get("requestData", {})
                self.requests.append((t, rdata))
                if self.mode == "badjson" and t == "GetRecordStatus":
                    c.sendall(_frame(1, b"{not json"))
                    return
                if self.mode == "badstatus":
                    self._send(c, {"op": 7, "d": {"requestType": t, "requestId": rid}})
                    continue
                # an event first, to prove the client skips non-matching messages
                self._send(c, {"op": 5, "d": {"eventType": "Noise", "eventIntent": 1}})
                res, code, com, rd = self.handle(t, rdata)
                st = {"result": res, "code": code}
                if com:
                    st["comment"] = com
                out = {"requestType": t, "requestId": rid, "requestStatus": st}
                if rd is not None:
                    out["responseData"] = rd
                self._send(c, {"op": 7, "d": out})
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass

    def handle(self, t, rd):
        if t in self.responses:
            return self.responses[t]
        s = self.state
        if t == "GetVersion":
            return True, 100, None, {"obsVersion": "30"}
        if t == "GetRecordStatus":
            return True, 100, None, {"outputActive": s["recording"], "outputDuration": 12500 if s["recording"] else 0}
        if t == "StartRecord":
            if s["recording"]:
                return False, 500, "Output running", None
            if not s["stuck_start"]:           # stuck_start = OBS accepted the request but the output never starts
                s["recording"] = True
            return True, 100, None, None
        if t == "StopRecord":
            if not s["recording"]:
                return False, 501, "Output not running", None
            s["recording"] = False
            return True, 100, None, {"outputPath": "C:/Videos/rec.mkv"}
        if t == "GetInputKindList":
            return True, 100, None, {"inputKinds": ["monitor_capture", "window_capture"] + s.get("audio_kinds", [
                "wasapi_input_capture", "wasapi_output_capture"])}
        if t == "SetInputMute":
            if rd["inputName"] in s.get("refuse_audio", ()):
                return False, 600, "Refused", None
            s.setdefault("mute", {})[rd["inputName"]] = rd["inputMuted"]
            return True, 100, None, None
        if t == "GetInputMute":
            return True, 100, None, {"inputMuted": s.setdefault("mute", {}).get(rd["inputName"], False)}
        if t == "SetInputAudioTracks":
            s.setdefault("tracks", {})[rd["inputName"]] = rd["inputAudioTracks"]
            return True, 100, None, None
        if t == "GetSpecialInputs":
            return True, 100, None, {"desktop1": "Desktop Audio", "desktop2": None, "mic1": "Mic/Aux", "mic2": None}
        if t == "SetInputSettings" and rd.get("inputName") == "AIDA Mic":
            s.setdefault("settings", {})["AIDA Mic"] = rd["inputSettings"]
            return True, 100, None, None
        if t == "GetInputPropertiesListPropertyItems" and rd.get("propertyName") == "device_id":
            if s.get("no_device_list"):
                return False, 604, "Property not found", None
            return True, 100, None, {"propertyItems": s.get("devices", [
                {"itemEnabled": True, "itemName": "Default", "itemValue": "default"},
                {"itemEnabled": True, "itemName": "Headset Mic", "itemValue": "{0.0.1}.{aaa}"},
                {"itemEnabled": False, "itemName": "Unplugged", "itemValue": "{0.0.1}.{bbb}"}])}
        if t == "GetSceneList":
            return True, 100, None, {"scenes": [{"sceneName": n} for n in s["scenes"]]}
        if t == "CreateScene":
            s["scenes"].append(rd["sceneName"])
            return True, 100, None, {"sceneUuid": "u"}
        if t == "GetInputList":
            return True, 100, None, {"inputs": [{"inputName": n} for n in s["inputs"]]}
        if t == "CreateInput":
            s["inputs"].append(rd["inputName"])
            s["items"].append(rd["inputName"])
            return True, 100, None, {"inputUuid": "i", "sceneItemId": 1}
        if t == "GetSceneItemList":
            return True, 100, None, {"sceneItems": [{"sourceName": n, "sceneItemId": i + 1}
                                                    for i, n in enumerate(s["items"])]}
        if t == "CreateSceneItem":
            s["items"].append(rd["sourceName"])
            return True, 100, None, {"sceneItemId": 2}
        if t == "GetInputPropertiesListPropertyItems":
            return True, 100, None, {"propertyItems": [
                {"itemName": "Mon A: 1920x1080 @ 0,0 (Primary Monitor)", "itemValue": "ID-A"},
                {"itemName": "Mon B: 1920x1080 @ 1920,0", "itemValue": "ID-B"}]}
        if t == "SetRecordDirectory":
            s["record_dir"] = rd["recordDirectory"]
            return True, 100, None, None
        if t == "GetVideoSettings":
            return True, 100, None, dict(s["video"])
        if t == "SetVideoSettings":
            s["video"].update(rd)
            return True, 100, None, None
        if t == "SetSceneItemTransform":
            s["transforms"][rd["sceneItemId"]] = rd["sceneItemTransform"]
            return True, 100, None, None
        if t == "SetSceneItemEnabled":
            s["enabled"][rd["sceneItemId"]] = rd["sceneItemEnabled"]
            return True, 100, None, None
        if t in ("SetInputSettings", "SetCurrentProgramScene"):
            return True, 100, None, None
        return False, 204, "Unknown request", None


def rec(srv, pw=""):
    # record_dir="" leaves OBS's own folder alone in the older tests; verify_secs kept short so a stuck start is quick
    return ObsRecorder("127.0.0.1", srv.port, pw, timeout=1.5, record_dir="", verify_secs=0.6, poll=0.05)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class T(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.srv = FakeObs()

    def tearDown(self):
        self.srv.close()

    def types(self):
        return [t for t, _ in self.srv.requests]

    def assertShape(self, r):
        self.assertIsInstance(r, dict)
        self.assertIsInstance(r["ok"], bool)
        self.assertIsInstance(r["error"], str)

    # --- auth ---
    def test_01_auth_vector_matches_independent_calc(self):
        # Spec: secret=b64(sha256(pw+salt)); auth=b64(sha256(secret+challenge)). Doc example salt/challenge.
        salt, ch = self.srv.salt, self.srv.challenge
        sec = base64.b64encode(hashlib.sha256(("supersecretpassword" + salt).encode()).digest()).decode()
        exp = base64.b64encode(hashlib.sha256((sec + ch).encode()).digest()).decode()
        self.assertEqual(make_auth("supersecretpassword", salt, ch), exp)
        self.assertEqual(len(base64.b64decode(exp)), 32)

    def test_02_auth_success(self):
        self.srv.password = "pw123"
        r = rec(self.srv, "pw123").status()
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["connected"])

    def test_03_auth_wrong_password(self):
        self.srv.password = "pw123"
        r = rec(self.srv, "nope").status()
        self.assertFalse(r["ok"])
        self.assertIn("password", r["error"].lower())

    def test_04_auth_required_but_none_given(self):
        self.srv.password = "pw123"
        r = rec(self.srv, "").ensure_ready()
        self.assertFalse(r["ok"])
        self.assertIn("password", r["error"].lower())

    def test_05_password_not_in_repr_or_errors(self):
        self.srv.password = "sekrit-value"
        o = rec(self.srv, "wrong-sekrit")
        self.assertNotIn("sekrit", repr(o))
        self.assertNotIn("sekrit", json.dumps(o.status()))

    # --- status ---
    def test_10_status_idle(self):
        r = rec(self.srv).status()
        self.assertTrue(r["ok"] and r["connected"] and r["obs_running"])
        self.assertFalse(r["recording"])
        self.assertEqual(r["seconds"], 0.0)

    def test_11_status_recording_seconds(self):
        self.srv.state["recording"] = True
        r = rec(self.srv).status()
        self.assertTrue(r["recording"])
        self.assertAlmostEqual(r["seconds"], 12.5)

    # --- start / stop ---
    def test_20_start_stop(self):
        o = rec(self.srv)
        r = o.start(audio_desktop=False, audio_mic=False)     # video-only test: no audio inputs wanted or created
        self.assertTrue(r["ok"], r)
        self.assertIn("path", r)
        self.assertNotIn("CreateScene", self.types())
        r = o.stop()
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "C:/Videos/rec.mkv")

    def test_21_start_when_already_recording(self):
        self.srv.state["recording"] = True
        r = rec(self.srv).start()
        self.assertFalse(r["ok"])
        self.assertIn("already", r["error"])

    def test_22_stop_when_not_recording(self):
        r = rec(self.srv).stop()
        self.assertFalse(r["ok"])
        self.assertEqual(r["path"], "")

    def test_23_request_error_generic_500(self):
        self.srv.responses["StartRecord"] = (False, 702, "Cancelled", None)
        r = rec(self.srv).start()
        self.assertFalse(r["ok"])
        self.assertIn("Cancelled", r["error"])

    def test_24_request_error_501_on_start_is_error(self):
        self.srv.responses["StartRecord"] = (False, 501, "nope", None)
        self.assertFalse(rec(self.srv).start()["ok"])

    # --- monitor switch ---
    def test_30_start_with_monitor_creates_scene_and_input(self):
        r = rec(self.srv).start(monitor_index=1)
        self.assertTrue(r["ok"], r)
        t = self.types()
        for need in ("CreateScene", "CreateInput", "SetInputSettings", "SetCurrentProgramScene", "StartRecord"):
            self.assertIn(need, t)
        self.assertLess(t.index("SetCurrentProgramScene"), t.index("StartRecord"))
        ci = [d for n, d in self.srv.requests if n == "CreateInput"][0]
        self.assertEqual(ci["inputName"], "AIDA Display")
        self.assertEqual(ci["inputKind"], "monitor_capture")
        si = [d for n, d in self.srv.requests if n == "SetInputSettings"][0]
        self.assertEqual(si["inputSettings"]["monitor"], 1)
        self.assertEqual(si["inputSettings"]["monitor_id"], "ID-B")

    def test_31_start_with_monitor_reuses_existing(self):
        self.srv.state.update(scenes=["AIDA Screen"], inputs=["AIDA Display"], items=["AIDA Display"])
        r = rec(self.srv).start(monitor_index=0, audio_desktop=False, audio_mic=False)
        self.assertTrue(r["ok"])
        self.assertNotIn("CreateScene", self.types())
        self.assertNotIn("CreateInput", self.types())

    def test_32_existing_input_not_in_scene_gets_scene_item(self):
        self.srv.state.update(scenes=["AIDA Screen"], inputs=["AIDA Display"], items=[])
        self.assertTrue(rec(self.srv).start(monitor_index=0)["ok"])
        self.assertIn("CreateSceneItem", self.types())

    def test_33_monitor_out_of_range(self):
        r = rec(self.srv).start(monitor_index=9)
        self.assertFalse(r["ok"])
        self.assertNotIn("StartRecord", self.types())

    def test_34_monitor_index_garbage(self):
        r = rec(self.srv).start(monitor_index="abc")
        self.assertFalse(r["ok"])
        self.assertShape(r)

    def test_35_list_monitors_obs_items_matched_to_rects(self):
        self.srv.state.update(scenes=["AIDA Screen"], inputs=["AIDA Display"])
        orig = obs_control.win_monitors
        obs_control.win_monitors = lambda: [{"index": 0, "name": "A", "rect": [0, 0, 1920, 1080]},
                                            {"index": 1, "name": "B", "rect": [1920, 0, 3840, 1080]}]
        try:
            r = rec(self.srv).list_monitors()
        finally:
            obs_control.win_monitors = orig
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["monitors"]), 2)
        self.assertEqual(r["monitors"][1]["rect"], [1920, 0, 3840, 1080])

    def test_36_list_monitors_falls_back_to_ctypes_when_obs_down(self):
        orig = obs_control.win_monitors
        obs_control.win_monitors = lambda: [{"index": 0, "name": "X", "rect": [0, 0, 10, 10]}]
        try:
            r = ObsRecorder("127.0.0.1", _free_port(), "", timeout=1).list_monitors()
        finally:
            obs_control.win_monitors = orig
        self.assertTrue(r["ok"])
        self.assertEqual(r["monitors"][0]["name"], "X")

    def test_37_win_monitors_real_call_does_not_raise(self):
        self.assertIsInstance(obs_control.win_monitors(), list)

    # --- not running / disabled / timeouts / malformed ---
    def test_40_connection_refused(self):
        o = ObsRecorder("127.0.0.1", _free_port(), "", timeout=1)
        r = o.status()
        self.assertFalse(r["ok"])
        self.assertFalse(r["connected"])
        self.assertFalse(r["obs_running"])
        self.assertTrue(r["error"])
        for fn in (o.start, o.stop, o.list_monitors):
            self.assertShape(fn())

    def test_41_websocket_disabled_http_reply(self):
        self.srv.mode = "notws"
        r = rec(self.srv).status()
        self.assertFalse(r["ok"])
        self.assertIn("websocket", r["error"].lower())

    def test_42_timeout_silent_server(self):
        self.srv.mode = "silent"
        o = ObsRecorder("127.0.0.1", self.srv.port, "", timeout=0.5)
        r = o.status()
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"].lower())

    def test_43_malformed_json_reply(self):
        self.srv.mode = "badjson"
        r = rec(self.srv).status()
        self.assertFalse(r["ok"])
        self.assertShape(r)

    def test_44_no_hello(self):
        self.srv.mode = "nohello"
        self.assertFalse(rec(self.srv).start()["ok"])

    def test_45_closed_after_hello(self):
        self.srv.mode = "closeafterhello"
        self.assertFalse(rec(self.srv).stop()["ok"])

    def test_46_response_without_status(self):
        self.srv.mode = "badstatus"
        r = rec(self.srv).start()
        self.assertFalse(r["ok"])
        self.assertIn("malformed", r["error"].lower())

    def test_47_bad_port_and_host_never_raise(self):
        for o in (ObsRecorder("no.such.host.invalid", 4455, "", timeout=1), ObsRecorder("127.0.0.1", "xx", None, timeout=1),
                  ObsRecorder(None, None, None, timeout=1)):
            for r in (o.status(), o.start(), o.stop(), o.list_monitors(), o.ensure_ready()):
                self.assertShape(r)

    # --- ensure_ready / config / find ---
    def test_50_ensure_ready_ok(self):
        orig = obs_control.find_obs
        obs_control.find_obs = lambda: "C:/x/obs64.exe"
        try:
            r = rec(self.srv).ensure_ready()
        finally:
            obs_control.find_obs = orig
        self.assertTrue(r["ok"], r)

    def test_51_ensure_ready_not_installed(self):
        orig = obs_control.find_obs
        obs_control.find_obs = lambda: ""
        try:
            r = rec(self.srv).ensure_ready()
        finally:
            obs_control.find_obs = orig
        self.assertFalse(r["ok"])
        self.assertIn("not installed", r["error"])

    def test_52_ensure_ready_not_running_vs_ws_disabled(self):
        o = ObsRecorder("127.0.0.1", _free_port(), "", timeout=1)
        of, op = obs_control.find_obs, obs_control._obs_running_process
        obs_control.find_obs = lambda: "C:/x/obs64.exe"
        try:
            obs_control._obs_running_process = lambda: False
            self.assertIn("not running", o.ensure_ready()["error"])
            obs_control._obs_running_process = lambda: True
            self.assertIn("WebSocket", o.ensure_ready()["error"])
        finally:
            obs_control.find_obs, obs_control._obs_running_process = of, op

    def test_53_from_config(self):
        o = obs_control.from_config({"obs_host": "h", "obs_port": "4466", "obs_password": "p"})
        self.assertEqual((o.host, o.port), ("h", 4466))
        o = obs_control.from_config({})
        self.assertEqual((o.host, o.port), ("localhost", 4455))
        self.assertIsInstance(obs_control.from_config(None), ObsRecorder)

    def test_54_find_obs_returns_str(self):
        self.assertIsInstance(obs_control.find_obs(), str)

    def test_55_launch_obs_not_installed_and_running_paths(self):
        of, op, pp = obs_control.find_obs, obs_control._obs_running_process, obs_control.subprocess.Popen
        calls = []
        obs_control.subprocess.Popen = lambda *a, **k: calls.append((a, k))
        try:
            obs_control._obs_running_process = lambda: True
            self.assertFalse(ObsRecorder().launch_obs()["launched"])
            obs_control._obs_running_process = lambda: False
            obs_control.find_obs = lambda: ""
            self.assertFalse(ObsRecorder().launch_obs()["ok"])
            obs_control.find_obs = lambda: "C:/x/y/obs64.exe"
            r = ObsRecorder().launch_obs()
            self.assertTrue(r["launched"])
            self.assertEqual(calls[0][1]["cwd"], "C:/x/y")
        finally:
            obs_control.find_obs, obs_control._obs_running_process = of, op
            obs_control.subprocess.Popen = pp


class StartStopFix(unittest.TestCase):
    """OBS start verification, record folder, two-screen canvas, hidden launch, monitor numbering."""

    def setUp(self):
        import os
        import tempfile
        reset_state()
        self.srv = FakeObs()
        self._vb = obs_control.VIDEO_BACKUP
        self._tmp = tempfile.mkdtemp()
        obs_control.VIDEO_BACKUP = os.path.join(self._tmp, "vb.json")     # never touch the real state\ file

    def tearDown(self):
        import shutil
        self.srv.close()
        obs_control.VIDEO_BACKUP = self._vb
        shutil.rmtree(self._tmp, ignore_errors=True)

    def types(self):
        return [t for t, _ in self.srv.requests]

    def test_60_start_is_verified_by_status_polling(self):
        # StartRecord is only 'accepted'; the client must then ask GetRecordStatus and see outputActive.
        r = rec(self.srv).start()
        self.assertTrue(r["ok"], r)
        t = self.types()
        self.assertGreater(t.index("GetRecordStatus", t.index("StartRecord")), t.index("StartRecord"))

    def test_61_start_that_never_begins_is_not_ok(self):
        # The screenshot bug: request accepted, output never active -> must be ok False with a reason, not success.
        self.srv.state["stuck_start"] = True
        r = rec(self.srv).start()
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])
        self.assertEqual(r["paths"], [])

    def test_62_stop_when_not_recording(self):
        r = rec(self.srv).stop()
        self.assertFalse(r["ok"])
        self.assertIn("not recording", r["error"])
        self.assertEqual(r["paths"], [])

    def test_63_stop_returns_path_and_paths(self):
        o = rec(self.srv)
        self.assertTrue(o.start()["ok"])
        r = o.stop()
        self.assertEqual((r["path"], r["paths"]), ("C:/Videos/rec.mkv", ["C:/Videos/rec.mkv"]))

    def test_64_record_dir_is_set_before_starting(self):
        # Controlled Folder Access blocks OBS writing to Videos: the recorder points OBS at a safe folder first.
        import os
        d = os.path.join(self._tmp, "recs")
        o = ObsRecorder("127.0.0.1", self.srv.port, "", timeout=1.5, record_dir=d, verify_secs=0.6, poll=0.05)
        self.assertTrue(o.start()["ok"])
        self.assertEqual(self.srv.state["record_dir"], d)
        self.assertTrue(os.path.isdir(d))
        self.assertLess(self.types().index("SetRecordDirectory"), self.types().index("StartRecord"))

    def test_65_default_record_dir_not_a_protected_folder(self):
        import os
        low = obs_control.DEFAULT_RECORD_DIR.lower()
        for bad in ("videos", "documents", "pictures", "desktop", "music"):
            self.assertNotIn(os.sep + bad, low)
        self.assertEqual(obs_control.from_config({"obs_record_dir": "D:/x"}).record_dir, "D:/x")
        self.assertEqual(obs_control.from_config({}).record_dir, obs_control.DEFAULT_RECORD_DIR)

    def test_66_two_monitors_one_wide_canvas_side_by_side_then_restored(self):
        o = rec(self.srv)
        r = o.start(monitor_indexes=[1, 0], audio_desktop=False, audio_mic=False)
        self.assertTrue(r["ok"], r)
        v = self.srv.state["video"]
        self.assertEqual((v["baseWidth"], v["baseHeight"], v["outputWidth"]), (3840, 1080, 3840))
        self.assertEqual(self.srv.state["inputs"], ["AIDA Display", "AIDA Display 2"])
        xs = sorted(t["positionX"] for t in self.srv.state["transforms"].values())
        self.assertEqual(xs, [0.0, 1920.0])
        self.assertTrue(o.stop()["ok"])
        self.assertEqual(self.srv.state["video"]["baseWidth"], 1920)       # original canvas put back

    def test_67_wide_canvas_output_capped_for_encoder(self):
        self.assertEqual(obs_control.MAX_ENCODE_WIDTH, 4096)
        orig = obs_control.parse_monitor_item
        obs_control.parse_monitor_item = lambda n: [0, 0, 2560, 1440] if "A:" in n else [2560, 0, 5120, 1440]
        try:
            self.assertTrue(rec(self.srv).start(monitor_indexes=[0, 1])["ok"])
        finally:
            obs_control.parse_monitor_item = orig
        v = self.srv.state["video"]
        self.assertEqual((v["baseWidth"], v["outputWidth"], v["outputHeight"]), (5120, 4096, 1152))

    def test_68_single_monitor_list_and_monitor_index_compat(self):
        self.assertTrue(rec(self.srv).start(monitor_indexes=[1])["ok"])
        si = [d for n, d in self.srv.requests if n == "SetInputSettings"][0]
        self.assertEqual(si["inputSettings"]["monitor_id"], "ID-B")
        self.assertNotIn("AIDA Display 2", self.srv.state["inputs"])

    def test_69_bad_monitor_indexes_reported(self):
        r = rec(self.srv).start(monitor_indexes=["x"])
        self.assertFalse(r["ok"])
        self.assertNotIn("StartRecord", self.types())

    def test_70_list_monitors_number_label_fields(self):
        r = rec(self.srv).list_monitors()
        self.assertTrue(r["ok"], r)
        m = r["monitors"]
        self.assertEqual([x["number"] for x in m], [1, 2])
        self.assertEqual(m[0]["label"], "Screen 1 (left) 1920x1080")
        self.assertEqual(m[1]["label"], "Screen 2 (right) 1920x1080")
        self.assertEqual(m[1]["rect"], [1920, 0, 3840, 1080])

    def test_71_number_monitors_orders_by_rect_not_index(self):
        out = obs_control.number_monitors([{"index": 0, "name": "r", "rect": [1920, 0, 3840, 1080]},
                                           {"index": 1, "name": "l", "rect": [0, 0, 1920, 1080]},
                                           {"index": 2, "name": "m", "rect": [3840, 0, 5760, 1080]}])
        self.assertEqual([x["number"] for x in out], [2, 1, 3])             # original order kept, numbers by position
        self.assertEqual(out[1]["label"], "Screen 1 (left) 1920x1080")
        self.assertEqual(out[0]["side"], "middle")

    def test_72_parse_monitor_item(self):
        self.assertEqual(obs_control.parse_monitor_item("S32E590C: 1920x1080 @ 1920,0"), [1920, 0, 3840, 1080])
        self.assertIsNone(obs_control.parse_monitor_item("nonsense"))

    def test_73_hidden_launch_flags_and_hider(self):
        # Real OBS flags only (no replay buffer), minimised no-activate window, hider started on the new pid.
        import time
        self.assertEqual(obs_control.OBS_LAUNCH_FLAGS, ["--minimize-to-tray", "--disable-shutdown-check"])
        saved = (obs_control.find_obs, obs_control._obs_running_process, obs_control.subprocess.Popen,
                 obs_control.hide_obs_windows)
        calls, hid = [], []

        class P:
            pid = 4242
        obs_control.subprocess.Popen = lambda *a, **k: (calls.append((a, k)), P())[1]
        obs_control.hide_obs_windows = lambda pid, *a, **k: hid.append(pid)
        try:
            obs_control._obs_running_process = lambda: False
            obs_control.find_obs = lambda: "C:/x/y/obs64.exe"
            self.assertTrue(ObsRecorder().launch_obs()["launched"])
            time.sleep(0.2)
            argv = calls[0][0][0]
            self.assertEqual(argv[1:], obs_control.OBS_LAUNCH_FLAGS)
            self.assertNotIn("--startreplaybuffer", argv)
            if obs_control.sys.platform == "win32":
                self.assertEqual(calls[0][1]["startupinfo"].wShowWindow, 7)
            self.assertEqual(hid, [4242])
            # already running: leave it alone, no new process, no hiding
            calls.clear()
            hid.clear()
            obs_control._obs_running_process = lambda: True
            self.assertFalse(ObsRecorder().launch_obs()["launched"])
            time.sleep(0.1)
            self.assertEqual((calls, hid), ([], []))
        finally:
            (obs_control.find_obs, obs_control._obs_running_process, obs_control.subprocess.Popen,
             obs_control.hide_obs_windows) = saved

    def test_74_hide_obs_windows_never_raises_on_bad_pid(self):
        self.assertEqual(obs_control.hide_obs_windows(0), 0)
        self.assertIsInstance(obs_control.hide_obs_windows(99999999, timeout=0.2), int)


class AudioOptions(unittest.TestCase):
    """Audio choice (PC sound / microphone), record folder and microphone list, against the fake v5 server only."""

    def setUp(self):
        import tempfile
        reset_state()
        self.srv = FakeObs()
        self._vb = obs_control.VIDEO_BACKUP
        self._tmp = tempfile.mkdtemp()
        obs_control.VIDEO_BACKUP = os.path.join(self._tmp, "vb.json")

    def tearDown(self):
        import shutil
        self.srv.close()
        obs_control.VIDEO_BACKUP = self._vb
        shutil.rmtree(self._tmp, ignore_errors=True)

    def reqs(self, name):
        return [d for t, d in self.srv.requests if t == name]

    def test_80_desktop_only_creates_input_unmuted_on_track_1(self):
        # Desktop audio on, mic off: AIDA Desktop Audio created with the real kind, unmuted, routed to track 1 only.
        r = rec(self.srv).start(monitor_index=0, audio_desktop=True, audio_mic=False)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["audio"], ["desktop"])
        self.assertEqual(r["warnings"], [])
        ci = [d for d in self.reqs("CreateInput") if d["inputName"] == "AIDA Desktop Audio"][0]
        self.assertEqual(ci["inputKind"], "wasapi_output_capture")
        self.assertEqual(ci["sceneName"], "AIDA Screen")
        self.assertEqual(ci["inputSettings"], {"device_id": "default"})
        self.assertIs(self.srv.state["mute"]["AIDA Desktop Audio"], False)
        tr = self.srv.state["tracks"]["AIDA Desktop Audio"]
        self.assertTrue(tr["1"])
        self.assertFalse(any(v for k, v in tr.items() if k != "1"))
        self.assertNotIn("AIDA Mic", self.srv.state["inputs"])          # not wanted: never created

    def test_81_mic_only_uses_input_capture_kind(self):
        r = rec(self.srv).start(monitor_index=0, audio_desktop=False, audio_mic=True)
        self.assertEqual(r["audio"], ["mic"])
        ci = [d for d in self.reqs("CreateInput") if d["inputName"] == "AIDA Mic"][0]
        self.assertEqual(ci["inputKind"], "wasapi_input_capture")
        self.assertNotIn("AIDA Desktop Audio", self.srv.state["inputs"])

    def test_82_both_and_neither(self):
        o = rec(self.srv)
        self.assertEqual(o.start(monitor_index=0, audio_desktop=True, audio_mic=True)["audio"], ["desktop", "mic"])
        self.assertIs(self.srv.state["mute"]["AIDA Mic"], False)
        o.stop()
        # Neither: sources are MUTED, not deleted, and the result says no audio.
        r = o.start(monitor_index=0, audio_desktop=False, audio_mic=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["audio"], [])
        self.assertIs(self.srv.state["mute"]["AIDA Desktop Audio"], True)
        self.assertIs(self.srv.state["mute"]["AIDA Mic"], True)
        self.assertNotIn("RemoveInput", self.types_())
        self.assertIn("AIDA Mic", self.srv.state["inputs"])

    def types_(self):
        return [t for t, _ in self.srv.requests]

    def test_83_existing_input_is_reused_not_recreated(self):
        self.srv.state["scenes"].append("AIDA Screen")
        self.srv.state["inputs"].append("AIDA Desktop Audio")
        self.srv.state["items"].append("AIDA Desktop Audio")
        r = rec(self.srv).start(monitor_index=0, audio_desktop=True, audio_mic=False)
        self.assertEqual(r["audio"], ["desktop"])
        self.assertEqual([d for d in self.reqs("CreateInput") if d["inputName"] == "AIDA Desktop Audio"], [])
        self.assertEqual(self.reqs("CreateSceneItem"), [])

    def test_84_existing_input_missing_from_scene_gets_scene_item(self):
        self.srv.state["scenes"].append("AIDA Screen")
        self.srv.state["inputs"].append("AIDA Mic")
        r = rec(self.srv).start(monitor_index=0, audio_desktop=False, audio_mic=True)
        self.assertEqual(r["audio"], ["mic"])
        self.assertIn({"sceneName": "AIDA Screen", "sourceName": "AIDA Mic"}, self.reqs("CreateSceneItem"))

    def test_85_obs_refuses_input_kind_video_still_records_with_warning(self):
        # Decision: audio trouble never costs the video. ok True, source absent from `audio`, reason in `warnings`.
        self.srv.state["audio_kinds"] = []                  # this OBS has no wasapi kinds
        r = rec(self.srv).start(monitor_index=0, audio_desktop=True, audio_mic=False)
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.srv.state["recording"])
        self.assertEqual(r["audio"], [])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("PC sound", r["warnings"][0])

    def test_86_obs_rejects_mute_request_warns_and_still_records(self):
        self.srv.state["refuse_audio"] = ["AIDA Mic"]
        r = rec(self.srv).start(monitor_index=0, audio_desktop=True, audio_mic=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["audio"], ["desktop"])
        self.assertIn("microphone", r["warnings"][0])

    def test_87_defaults_come_from_constructor_when_nothing_saved(self):
        # No audio args + nothing remembered = config defaults (desktop on, mic off).
        r = rec(self.srv).start(monitor_index=0)
        self.assertEqual(r["audio"], ["desktop"])
        reset_state()
        o = ObsRecorder("127.0.0.1", self.srv.port, "", timeout=1.5, record_dir="", verify_secs=0.6, poll=0.05,
                        audio_desktop=False, audio_mic=True)
        self.assertEqual(o.audio_prefs(), {"desktop": False, "mic": True})
        self.assertEqual(obs_control.from_config({"obs_audio_desktop": False, "obs_audio_mic": True}).audio_prefs(),
                         {"desktop": False, "mic": True})
        self.assertEqual(obs_control.from_config({}).audio_prefs(), {"desktop": True, "mic": False})

    def test_88_last_choice_is_remembered_and_used_next_time(self):
        o = rec(self.srv)
        o.start(monitor_index=0, audio_desktop=False, audio_mic=True)
        o.stop()
        self.assertEqual(o.audio_prefs(), {"desktop": False, "mic": True})
        self.assertEqual(rec(self.srv).start(monitor_index=0)["audio"], ["mic"])   # fresh object, plain start

    def test_89_failed_start_does_not_overwrite_saved_choice(self):
        o = rec(self.srv)
        o.save_prefs(desktop=True, mic=False)
        self.srv.state["stuck_start"] = True
        self.assertFalse(o.start(monitor_index=0, audio_desktop=False, audio_mic=True)["ok"])
        self.assertEqual(o.audio_prefs(), {"desktop": True, "mic": False})

    def test_90_remember_false_keeps_saved_choice(self):
        o = rec(self.srv)
        o.save_prefs(desktop=True, mic=True)
        o.start(monitor_index=0, audio_desktop=False, audio_mic=False, remember=False)
        self.assertEqual(o.audio_prefs(), {"desktop": True, "mic": True})

    def test_91_master_audio_off_default_gives_silent_plain_start(self):
        o = rec(self.srv)
        o.save_prefs(audio=False, desktop=True, mic=True)
        r = o.start(monitor_index=0)
        self.assertEqual(r["audio"], [])
        self.assertEqual(o.prefs()["desktop"], True)             # the ticks themselves survive

    def test_92_global_obs_audio_muted_during_recording_and_restored(self):
        # OBS ships global 'Desktop Audio' and 'Mic/Aux': left live they would defeat 'record without sound'.
        o = rec(self.srv)
        o.start(monitor_index=0, audio_desktop=False, audio_mic=False)
        self.assertIs(self.srv.state["mute"]["Desktop Audio"], True)
        self.assertIs(self.srv.state["mute"]["Mic/Aux"], True)
        self.assertTrue(o.stop()["ok"])
        self.assertIs(self.srv.state["mute"]["Desktop Audio"], False)   # back to how they were
        self.assertIs(self.srv.state["mute"]["Mic/Aux"], False)
        self.assertFalse(os.path.isfile(os.path.join(obs_control.STATE_DIR, "obs_audio_backup.json")))

    def test_93_global_prior_mute_state_is_preserved(self):
        self.srv.state["mute"] = {"Mic/Aux": True}
        o = rec(self.srv)
        o.start(monitor_index=0, audio_desktop=True, audio_mic=False)
        o.stop()
        self.assertIs(self.srv.state["mute"]["Mic/Aux"], True)
        self.assertIs(self.srv.state["mute"]["Desktop Audio"], False)

    def test_94_failed_start_restores_globals(self):
        self.srv.state["stuck_start"] = True
        rec(self.srv).start(monitor_index=0, audio_desktop=False, audio_mic=False)
        self.assertIs(self.srv.state["mute"]["Desktop Audio"], False)

    def test_95_mic_device_applied_with_device_id(self):
        o = rec(self.srv)
        o.start(monitor_index=0, audio_desktop=False, audio_mic=True, mic_device="{0.0.1}.{aaa}")
        self.assertEqual(self.srv.state["settings"]["AIDA Mic"], {"device_id": "{0.0.1}.{aaa}"})
        self.assertEqual(o.prefs()["mic_device"], "{0.0.1}.{aaa}")
        o.stop()
        o.start(monitor_index=0, audio_desktop=False, audio_mic=True)           # remembered
        self.assertEqual(self.srv.state["settings"]["AIDA Mic"], {"device_id": "{0.0.1}.{aaa}"})
        o.stop()
        o.start(monitor_index=0, audio_desktop=False, audio_mic=True, mic_device="")   # blank = Windows default
        self.assertEqual(self.srv.state["settings"]["AIDA Mic"], {"device_id": "default"})

    def test_96_already_recording_touches_no_audio(self):
        self.srv.state["recording"] = True
        r = rec(self.srv).start(monitor_index=0, audio_desktop=True, audio_mic=True)
        self.assertFalse(r["ok"])
        for t in ("CreateInput", "SetInputMute", "SetInputAudioTracks", "SetCurrentProgramScene"):
            self.assertNotIn(t, self.types_())

    # -- record folder --
    def test_97_saved_folder_applied_with_SetRecordDirectory_before_StartRecord(self):
        d = os.path.join(self._tmp, "chosen")
        o = rec(self.srv)
        r = o.start(monitor_index=0, record_dir=d)
        self.assertTrue(r["ok"])
        self.assertEqual(self.srv.state["record_dir"], d)
        self.assertLess(self.types_().index("SetRecordDirectory"), self.types_().index("StartRecord"))
        o.stop()
        self.assertEqual(o.prefs()["record_dir"], d)                 # persisted
        o2 = rec(self.srv)
        o2.start(monitor_index=0)                                    # next time, no argument
        self.assertEqual(self.srv.state["record_dir"], d)

    def test_98_blank_saved_folder_falls_back_to_configured_default(self):
        o = ObsRecorder("127.0.0.1", self.srv.port, "", timeout=1.5, record_dir=os.path.join(self._tmp, "dflt"),
                        verify_secs=0.6, poll=0.05)
        o.save_prefs(record_dir="")
        self.assertEqual(o.prefs()["record_dir"], os.path.join(self._tmp, "dflt"))

    def test_99_blocked_folder_error_surfaces_from_start(self):
        # The existing verify polling reports the real OBS reason; a stuck start must come back ok False with text.
        self.srv.state["stuck_start"] = True
        r = rec(self.srv).start(monitor_index=0, record_dir=os.path.join(self._tmp, "x"))
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_100_folder_warning_protected_and_writable(self):
        home = os.path.join(self._tmp, "home")
        os.makedirs(os.path.join(home, "Videos"))
        os.makedirs(os.path.join(home, "AIDA Recordings"))
        w = obs_control.folder_warning
        self.assertIn("Windows may block OBS", w(os.path.join(home, "Videos"), home=home))
        self.assertIn("Windows may block OBS", w(os.path.join(home, "Documents", "sub", "deeper"), home=home))
        self.assertIn("Windows may block OBS", w(os.path.join(home, "OneDrive", "Desktop"), home=home))
        self.assertIn("allow obs64.exe in Windows Security", w(os.path.join(home, "Music"), home=home))
        self.assertEqual(w(os.path.join(home, "AIDA Recordings"), home=home), "")
        self.assertEqual(w(os.path.join(home, "new", "folder"), home=home), "")   # not created yet but creatable
        self.assertEqual(w("", home=home), "")
        self.assertEqual(w(os.path.join(home, "Videosnot"), home=home), "")       # prefix is not a match

    def test_101_folder_warning_unwritable(self):
        f = os.path.join(self._tmp, "a_file.txt")
        open(f, "w").close()
        self.assertIn("not writable", obs_control.folder_warning(os.path.join(f, "sub"), home=self._tmp + "x"))

    # -- microphone list --
    def test_102_parse_device_items(self):
        items = [{"itemEnabled": True, "itemName": "Mic B", "itemValue": "b"},
                 {"itemEnabled": True, "itemName": "Default", "itemValue": "default"},
                 {"itemEnabled": False, "itemName": "Off", "itemValue": "off"},
                 {"itemEnabled": True, "itemName": "dup", "itemValue": "b"}, "junk", {"itemName": "no value"}]
        self.assertEqual(obs_control.parse_device_items(items),
                         [{"id": "default", "name": "Default"}, {"id": "b", "name": "Mic B"}])
        self.assertEqual(obs_control.parse_device_items([]), [{"id": "default", "name": "Default"}])
        self.assertEqual(obs_control.parse_device_items(None), [{"id": "default", "name": "Default"}])

    def test_103_list_mic_devices_from_obs(self):
        r = rec(self.srv).list_mic_devices()
        self.assertTrue(r["ok"], r)
        self.assertEqual([d["name"] for d in r["devices"]], ["Default", "Headset Mic"])    # disabled one dropped
        self.assertEqual(r["devices"][1]["id"], "{0.0.1}.{aaa}")
        self.assertIn("AIDA Mic", self.srv.state["inputs"])        # OBS had no mic input at all: one was made to ask

    def test_104_list_mic_devices_prefers_existing_input_without_creating(self):
        self.srv.state["inputs"].append("AIDA Mic")
        r = rec(self.srv).list_mic_devices()
        self.assertTrue(r["ok"])
        self.assertEqual(self.reqs("CreateInput"), [])

    def test_105_list_mic_devices_falls_back_to_default_only(self):
        self.srv.state["no_device_list"] = True
        r = rec(self.srv).list_mic_devices()
        self.assertFalse(r["ok"])
        self.assertEqual(r["devices"], [{"id": "default", "name": "Default"}])

    def test_106_list_mic_devices_obs_down(self):
        o = ObsRecorder("127.0.0.1", _free_port(), "", timeout=0.5)
        r = o.list_mic_devices()
        self.assertFalse(r["ok"])
        self.assertEqual(r["devices"], [{"id": "default", "name": "Default"}])


class RectMapping(unittest.TestCase):
    """Screen choice is matched by geometry, never by assumed index order (bug: Screen 1 recorded Screen 2)."""
    PRIMARY = {"itemName": "LQ: 1920x1080 @ 0,0 (Primary Monitor)", "itemValue": "p"}
    RIGHT = {"itemName": "S32: 1920x1080 @ 1920,0", "itemValue": "r"}
    LEFTNEG = {"itemName": "X: 1280x1024 @ -1280,0", "itemValue": "l"}

    def test_index_for_rect_any_enumeration_order(self):
        for items in ([self.RIGHT, self.PRIMARY], [self.PRIMARY, self.RIGHT]):
            i = obs_control.obs_index_for_rect(items, [0, 0, 1920, 1080])
            self.assertEqual(items[i]["itemValue"], "p")

    def test_negative_coordinates_monitor_left_of_primary(self):
        items = [self.PRIMARY, self.LEFTNEG]
        self.assertEqual(obs_control.obs_index_for_rect(items, [-1280, 0, 0, 1024]), 1)

    def test_mismatch_gives_clear_error(self):
        with self.assertRaises(obs_control.ObsError) as c:
            obs_control.obs_index_for_rect([self.PRIMARY], [5000, 0, 6920, 1080])
        self.assertIn("no screen at", c.exception.msg)

    def test_match_item_rect_only_when_unambiguous(self):
        mons = [{"rect": [0, 0, 1920, 1080]}, {"rect": [1920, 0, 3840, 1080]}]
        self.assertIsNone(obs_control.match_item_rect("Monitor 1920x1080", mons))
        self.assertEqual(obs_control.match_item_rect("M 1280x1024", mons + [{"rect": [-1280, 0, 0, 1024]}]), [-1280, 0, 0, 1024])


class AudioCheckTests(unittest.TestCase):
    """Post-recording sound check (10 Oct 22:43: the user's 'with sound' file played silent)."""

    def test_parse_loud(self):
        # Normal speech-level recording: has audio, not silent
        r = obs_control.parse_volumedetect("[x] mean_volume: -33.4 dB\n[x] max_volume: -0.9 dB\n")
        self.assertTrue(r["has_audio"]); self.assertFalse(r["silent"]); self.assertEqual(r["max_db"], -0.9)

    def test_parse_digital_silence(self):
        # Every sample zero: ffmpeg prints -inf, which counts as silent
        r = obs_control.parse_volumedetect("mean_volume: -inf dB\nmax_volume: -inf dB\n")
        self.assertTrue(r["has_audio"]); self.assertTrue(r["silent"])

    def test_parse_quiet_noise_floor_is_silent(self):
        # Peak under -60 dB = nothing audible
        self.assertTrue(obs_control.parse_volumedetect("mean_volume: -74.0 dB\nmax_volume: -65.2 dB")["silent"])

    def test_parse_no_audio_stream(self):
        # No volumedetect output at all = no audio stream
        r = obs_control.parse_volumedetect("Output file is empty, nothing was encoded")
        self.assertFalse(r["has_audio"]); self.assertTrue(r["silent"])

    def test_check_audio_without_ffmpeg_reports_unchecked(self):
        # Missing ffmpeg must say 'not checked', never claim silence was verified
        self.assertFalse(obs_control.check_audio(__file__, ffmpeg="")["checked"])

    def test_check_audio_missing_file(self):
        self.assertFalse(obs_control.check_audio("C:/nope/none.mp4", ffmpeg="ffmpeg")["checked"])


if __name__ == "__main__":
    unittest.main()
