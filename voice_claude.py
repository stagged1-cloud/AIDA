"""
claude2claude voice
Press the toggle key, talk, press it again. Claude Code does the job on this PC
and talks back through a Windows voice.

Run with start.bat. Settings live in config.yaml (copy of config.example.yaml).
"""
import os
import re
import sys
import json
import time
import queue
import random
import shutil
import ctypes
import sqlite3
import signal
import pathlib
import datetime
import threading
import collections
import subprocess

import yaml

HERE = pathlib.Path(__file__).resolve().parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8")) or {}
HOME = HERE / "assistant"                 # Claude Code works from here (CLAUDE.md, hooks)
STATE_FILE = HERE / "state.json"
TOKEN_FILE = HOME / ".confirm_token"      # read by assistant/guard.py
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
USER = str(CFG.get("user_name") or "You").strip() or "You"      # how you appear in the log and memory files


# ---------------------------------------------------------------- utilities
def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _enable_ansi():
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


_enable_ansi()
ESC = chr(27)


def show(who, text):
    """Console layout: you and TARS stand out, working-out lines are dim grey."""
    t = now()
    if who == USER:
        print(f"\n{ESC}[1;96m  YOU  {ESC}[2;96m{t}{ESC}[0m\n{ESC}[96m  {text}{ESC}[0m", flush=True)
    elif who == "Claude":
        print(f"\n{ESC}[1;92m  TARS  {ESC}[2;92m{t}{ESC}[0m\n{ESC}[38;5;151m  {text}{ESC}[0m", flush=True)
    else:
        try:
            width = shutil.get_terminal_size((120, 30)).columns - 14
        except Exception:
            width = 106
        short = str(text).replace("\n", " ")
        if len(short) > width:
            short = short[:width - 1] + "..."
        print(f"{ESC}[90m    {t} {who}: {short}{ESC}[0m", flush=True)


def log(who, text):
    line = f"[{now()}] {who}: {text}"
    show(who, text)
    try:
        day = datetime.date.today().isoformat()
        with open(LOG_DIR / f"{day}.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def beep(kind):
    try:
        import winsound
        if kind == "start":
            winsound.Beep(880, 90)
        elif kind == "stop":
            winsound.Beep(600, 90)
        elif kind == "error":
            winsound.Beep(300, 200)
    except Exception:
        pass


# ---------------------------------------------------------------- ears (mic + speech-to-text)
def add_cuda_dlls():
    """Let faster-whisper find the CUDA libraries installed by pip (nvidia-*)."""
    try:
        import nvidia  # namespace package from nvidia-cublas-cu12 / nvidia-cudnn-cu12
        for base in list(nvidia.__path__):
            for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
                d = pathlib.Path(base) / sub / "bin"
                if d.exists():
                    os.add_dll_directory(str(d))
                    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


def prep_audio(np, audio):
    """Quiet laptop mics: lift the recording to a healthy level before speech recognition (config: mic_normalise,
    mic_max_gain, mic_gain). Loud headset recordings are left alone."""
    if len(audio) == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if CFG.get("mic_normalise", True) and 0.001 < peak < 0.3:
        audio = audio * min(0.5 / peak, float(CFG.get("mic_max_gain", 20)))
    gain = float(CFG.get("mic_gain", 1.0))
    if gain != 1.0:
        audio = audio * gain
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


class Ears:
    def __init__(self):
        import numpy as np
        import sounddevice as sd
        self.np, self.sd = np, sd
        self.device = self._find_mic(CFG.get("microphone", ""))
        info = sd.query_devices(self.device, "input") if self.device is not None else sd.query_devices(kind="input")
        self.mic_name = info["name"]
        self.rate = int(info["default_samplerate"])
        self.frames, self.stream = [], None
        self.level = 0.0
        self._load_model()

    def _find_mic(self, want):
        if not want:
            return None
        for i, d in enumerate(self.sd.query_devices()):
            if d["max_input_channels"] > 0 and want.lower() in d["name"].lower():
                return i
        log("setup", f"Microphone '{want}' not found, using the Windows default.")
        return None

    def _load_model(self):
        add_cuda_dlls()
        from faster_whisper import WhisperModel
        silence = self.np.zeros(16000, dtype=self.np.float32)
        if str(CFG.get("whisper_device", "auto")).lower() == "cpu":
            self.model = WhisperModel(CFG.get("whisper_cpu_model", "small.en"), device="cpu", compute_type="int8")
            self.where = "CPU"
            return
        try:
            m = WhisperModel(CFG.get("whisper_model", "small.en"), device="cuda", compute_type="float16")
            list(m.transcribe(silence, language="en")[0])   # proves CUDA really works
            self.model, self.where = m, "GPU"
        except Exception as e:
            log("setup", f"GPU speech recognition unavailable ({e.__class__.__name__}), using CPU.")
            self.model = WhisperModel(CFG.get("whisper_cpu_model", "small.en"), device="cpu", compute_type="int8")
            self.where = "CPU"

    def start(self):
        self.frames = []
        self.level = 0.0
        np = self.np

        def on_audio(data, n, t, s):
            self.frames.append(data.copy())
            self.level = float(np.sqrt(np.mean(np.square(data))))

        self.stream = self.sd.InputStream(
            device=self.device, channels=1, samplerate=self.rate, dtype="float32", callback=on_audio)
        self.stream.start()

    def stop(self):
        np = self.np
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.level = 0.0
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self.frames)[:, 0]
        if self.rate != 16000:
            n = int(len(audio) * 16000 / self.rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        return audio

    def transcribe(self, audio):
        audio = prep_audio(self.np, audio)
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, vad_filter=True,
            vad_parameters={"threshold": float(CFG.get("vad_threshold", 0.3)),
                            "speech_pad_ms": int(CFG.get("vad_pad_ms", 400))},
            initial_prompt=(CFG.get("vocabulary", "") + ", " + learned_vocabulary()).strip(", "))
        return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------- mouth (Windows SAPI voice)
class Mouth(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.busy = False
        self.voice_name = "?"
        self.kokoro = None                     # neural voice (tts_engine: kokoro); None = Windows SAPI voice
        self.ready = threading.Event()
        self.start()
        self.ready.wait(15)

    @property
    def speaking(self):
        return self.busy or not self.q.empty()

    def say(self, text):
        text = (text or "").strip()
        if text:
            self.q.put(text)

    def stop(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.stop_evt.set()

    def run(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        v = win32com.client.Dispatch("SAPI.SpVoice")
        voices = v.GetVoices()
        names = [voices.Item(i).GetDescription() for i in range(voices.Count)]
        chosen = None
        for want in CFG.get("voice_prefer", []):
            for i, n in enumerate(names):
                if want.lower() in n.lower():
                    chosen = i
                    break
            if chosen is not None:
                break
        if chosen is not None:
            v.Voice = voices.Item(chosen)
        v.Rate = int(CFG.get("voice_rate", 0))
        self.voice_name = v.Voice.GetDescription()
        self.fx = None
        if CFG.get("tars_voice", False):
            try:
                from tars_fx import tars
                self.fx = tars
                self.voice_name += " + TARS processing"
            except Exception as e:
                log("setup", f"TARS processing unavailable ({e}), using the plain voice.")
        self._load_kokoro()
        self.ready.set()
        while True:
            text = self.q.get()
            self.stop_evt.clear()
            self.busy = True
            if self.kokoro:
                try:
                    self._speak_kokoro(text)
                except Exception as e:
                    log("voice", f"Kokoro voice failed ({e}), falling back to the Windows voice.")
                    self.kokoro = None
                else:
                    self.busy = False
                    continue
            if self.fx:
                try:
                    self._speak_fx(v, text)
                except Exception as e:
                    log("voice", f"TARS voice failed ({e}), falling back.")
                    self.fx = None
                self.busy = False
                continue
            try:
                v.Speak(text, 1)                      # 1 = async
                while not v.WaitUntilDone(100):
                    if self.stop_evt.is_set():
                        v.Speak("", 3)                # async + purge = stop now
                        break
                    pythoncom.PumpWaitingMessages()
            except Exception as e:
                log("voice", f"Speech failed: {e}")
            self.busy = False


    def _load_kokoro(self):
        """Kokoro neural voice, run locally. Any problem here leaves the Windows voice in charge."""
        if str(CFG.get("tts_engine", "sapi")).lower() != "kokoro":
            return
        try:
            from kokoro_onnx import Kokoro
            models = HERE / "models"
            k = Kokoro(str(models / "kokoro-v1.0.onnx"), str(models / "voices-v1.0.bin"))
            self.kvoice = CFG.get("kokoro_voice", "am_onyx")
            k.create("Ready.", voice=self.kvoice, speed=1.0, lang=CFG.get("kokoro_lang", "en-us"))      # warm up
            self.kokoro = k
            self.voice_name = f"Kokoro {self.kvoice}" + (" + TARS processing" if self.fx else "")
            self.kcache = {}
            threading.Thread(target=self._prerender, daemon=True).start()
        except Exception as e:
            self.kokoro = None
            log("setup", f"Kokoro voice unavailable ({e}), using the Windows voice. "
                         "(Models missing? Run install.ps1 again.)")

    def _prerender(self):
        """The program's own stock phrases, rendered once at startup so they play instantly."""
        phrases = ["Online.", "Didn't catch that.", "Cancelled.", "Still at it.", "Nothing to repeat yet.",
                   "Still on the last one. Say cancel if you want me to drop it.",
                   "That failed. The details are in the window."] + list(ACKS)
        for p in phrases:
            try:
                self.kcache[p] = self._kclip(p)
            except Exception:
                pass

    def _kclip(self, chunk):
        """One finished clip: Kokoro render, optional question lift, TARS filter, dead air trimmed off the front."""
        import numpy as np
        from tars_fx import wants_rise, rise_end
        short = len(chunk.split()) <= 4
        speed = float(CFG.get("kokoro_short_speed", 1.2) if short else CFG.get("kokoro_speed", 1.0))
        a, sr = self.kokoro.create(chunk, voice=self.kvoice, speed=speed, lang=CFG.get("kokoro_lang", "en-us"))
        a = np.asarray(a, dtype=np.float32)
        semis = float(CFG.get("kokoro_question_rise", 0))
        if semis and wants_rise(re.split(r"(?<=[.!])\s", chunk)[-1]):
            a = rise_end(a, sr, semis=semis)
        if self.fx:
            a = self.fx(a, sr, pitch=float(CFG.get("tars_pitch", 0.94)), metal=float(CFG.get("tars_metal", 0.28)),
                        low=float(CFG.get("tars_low", 260)), high=float(CFG.get("tars_high", 4200)))
        a = np.asarray(a, np.float32)
        loud = np.where(np.abs(a) > 0.02)[0]
        if len(loud):
            a = a[max(0, loud[0] - int(sr * 0.02)):]
        return np.concatenate([a, np.zeros(int(sr * 0.08), np.float32)])

    def _speak_kokoro(self, text):
        """Render a sentence or two ahead and stream them back to back, so the speech runs on without gaps."""
        import numpy as np
        import sounddevice as sd
        from tars_fx import wants_rise, rise_end
        parts = [p.strip() for p in re.findall(r"[^.!?]+[.!?]*", text) if p.strip()] or [text]
        chunks, cur = [parts[0]], ""                   # first sentence alone so it starts talking quickly
        for p in parts[1:]:
            if cur and len(cur) + len(p) > 300:
                chunks.append(cur.strip())
                cur = ""
            cur += " " + p
        if cur.strip():
            chunks.append(cur.strip())
        rate = 24000                                    # Kokoro's output rate
        buf = queue.Queue()
        left = [np.zeros(0, np.float32)]
        finished = threading.Event()

        def feed(out, frames, t, status):
            out.fill(0)
            filled = 0
            while filled < frames:
                if not len(left[0]):
                    try:
                        nxt = buf.get_nowait()
                    except queue.Empty:
                        return
                    if nxt is None:
                        finished.set()
                        return
                    left[0] = nxt
                n = min(frames - filled, len(left[0]))
                out[filled:filled + n, 0] = left[0][:n]
                left[0] = left[0][n:]
                filled += n

        cached = getattr(self, "kcache", {}).get(text.strip())
        with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", callback=feed):
            if cached is not None:
                buf.put(cached)                          # stock phrase: already rendered, plays instantly
                chunks = []
            for chunk in chunks:
                if self.stop_evt.is_set():
                    break
                buf.put(self._kclip(chunk))
            buf.put(None)
            while not finished.is_set():
                if self.stop_evt.wait(0.05):
                    break
            if not self.stop_evt.is_set():
                time.sleep(0.15)                        # let the last bit leave the speakers

    def _render(self, v, text, path):
        import win32com.client
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 26                                  # 24 kHz, 16-bit, mono (the neural voice's native rate)
        fs = win32com.client.Dispatch("SAPI.SpFileStream")
        fs.Format = fmt
        fs.Open(str(path), 3, False)
        v.AudioOutputStream = fs
        try:
            v.Speak(text)
        finally:
            fs.Close()
            v.AudioOutputStream = None

    def _speak_fx(self, v, text):
        """Render each sentence, run it through the TARS filter, play it; render the next while one plays."""
        import wave
        import winsound
        import numpy as np
        tmp = HERE / "tmp"
        tmp.mkdir(exist_ok=True)
        parts = re.findall(r"[^.!?]+[.!?]*", text) or [text]
        # first sentence alone so it starts talking quickly; the rest in long runs so the rhythm flows.
        # a yes/no question gets its own run so its ending can lift.
        from tars_fx import wants_rise, rise_end
        chunks, cur = [parts[0].strip()], ""
        for part in parts[1:]:
            if wants_rise(part):
                if cur.strip():
                    chunks.append(cur.strip())
                chunks.append(part.strip())
                cur = ""
                continue
            limit = 220 if len(chunks) == 1 else 700   # short second run: ready before the first sentence ends
            if len(cur) + len(part) < limit:
                cur += part
            else:
                if cur.strip():
                    chunks.append(cur.strip())
                cur = part
        if cur.strip():
            chunks.append(cur.strip())
        play_until = 0.0
        for i, chunk in enumerate(chunks):
            if self.stop_evt.is_set():
                break
            raw, out = tmp / "raw.wav", tmp / f"fx{i % 3}.wav"
            self._render(v, chunk, raw)
            with wave.open(str(raw)) as w:
                rate = w.getframerate()
                a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            if wants_rise(chunk.split(".")[-1] if not chunk.endswith("?") else re.split(r"(?<=[.!])\s", chunk)[-1]):
                a = rise_end(a, rate, semis=float(CFG.get("question_rise", 2.5)))
            y = self.fx(a, rate, pitch=float(CFG.get("tars_pitch", 0.94)), metal=float(CFG.get("tars_metal", 0.28)),
                        low=float(CFG.get("tars_low", 260)), high=float(CFG.get("tars_high", 4200)))
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
            while time.time() < play_until:            # let the previous sentence finish
                if self.stop_evt.wait(0.05):
                    break
            if self.stop_evt.is_set():
                break
            winsound.PlaySound(str(out), winsound.SND_FILENAME | winsound.SND_ASYNC)
            play_until = time.time() + len(y) / rate
        while time.time() < play_until:
            if self.stop_evt.wait(0.05):
                break
        if self.stop_evt.is_set():
            winsound.PlaySound(None, 0)                # stop now


def for_speech(text):
    """Turn Claude's reply into something worth hearing."""
    t = text or ""
    t = re.sub(r"```.*?```", " The details are on screen. ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*|__", "", t)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"https?://\S+?(?=[.,;:!?)]?(\s|$))", "the link", t)
    t = re.sub(r"[A-Za-z]:\\[^\s,]+?(?=[.,;:!?)]?(\s|$))", "that folder", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- brain (Claude Code)
class Brain:
    """Claude Code, kept running between requests. One-off runs for 'think hard' and connector jobs."""

    def __init__(self, state):
        self.state = state
        self.proc = None            # the always-running session
        self.events = None
        self.oneoff = None
        self.cancelled = False
        self.claude = CFG.get("claude_path") or shutil.which("claude") or str(pathlib.Path.home() / ".local/bin/claude.exe")

    # -- lifecycle
    def _kill(self, p):
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            return True
        return False

    def stop(self):
        p, self.proc = self.proc, None
        self._kill(p)

    def reset(self):
        """Fresh conversation. Today's memory file is loaded into the new one automatically."""
        self.stop()
        for k in ("session", "session_at", "session_day"):
            self.state.pop(k, None)
        save_state(self.state)

    def cancel(self):
        killed = False
        for p in (self.proc, self.oneoff):
            if self._kill(p):
                killed = True
        if killed:
            self.cancelled = True
            self.proc = None
        return killed

    def _fresh_session_ok(self):
        idle = time.time() - float(self.state.get("session_at", 0)) > float(CFG.get("idle_minutes", 30)) * 60
        new_day = self.state.get("session_day") != datetime.date.today().isoformat()
        return not idle and not new_day

    def _spawn(self, cmd, stdin):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, cwd=str(HOME), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=flags, bufsize=1)
        q = queue.Queue()
        proc.err_lines = []

        def read_out():
            for line in proc.stdout:
                line = line.strip()
                if line:
                    try:
                        q.put(json.loads(line))
                    except ValueError:
                        pass
            q.put(None)

        threading.Thread(target=read_out, daemon=True).start()
        threading.Thread(target=lambda: proc.err_lines.extend(proc.stderr), daemon=True).start()
        return proc, q

    def _base(self, model, mcp):
        cmd = [self.claude, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if not mcp:
            cmd += ["--strict-mcp-config"]          # skip loading connectors: saves seconds
        return cmd

    def _ensure(self, on_event):
        if self.proc and self.proc.poll() is None and not self._fresh_session_ok():
            self.stop()                              # idle too long or a new day: start clean
        if self.proc is None or self.proc.poll() is not None:
            cmd = self._base(CFG.get("model", "sonnet"), False) + ["--input-format", "stream-json"]
            if self.state.get("session") and self._fresh_session_ok():
                cmd += ["--resume", self.state["session"]]
            else:
                on_event("mode", "new conversation (today's memory loaded)")
                for k in ("session", "session_at", "session_day"):
                    self.state.pop(k, None)
            self.proc, self.events = self._spawn(cmd, subprocess.PIPE)

    # -- one request
    def ask(self, text, on_event=lambda kind, info: None, mode="normal"):
        humour = self.state.get("humour", CFG.get("humour", 60))
        honesty = self.state.get("honesty", CFG.get("honesty", 90))
        mirror = self.state.get("mirror", CFG.get("mirror", 40))
        where = str(CFG.get("time_label", "") or "").strip()
        msg = (f"[{datetime.datetime.now():%a %d %b %Y %H:%M}{(' ' + where) if where else ''} | user {USER} | "
               f"{CFG.get('language_style', 'British English')} | humour {humour}% | honesty {honesty}% | "
               f"mirror {mirror}%]\n{text}")
        self.cancelled = False
        if mode != "normal":
            return self._oneoff(msg, on_event, mode)
        for attempt in (1, 2):
            self._ensure(on_event)
            proc = self.proc
            try:
                proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": msg}}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                self.stop()
                continue
            reply, err, died = self._collect(self.events, proc, on_event)
            if died and reply is None and not self.cancelled and attempt == 1:
                self.stop()
                if err and re.search(r"no conversation|session not found|resume", err, re.I):
                    self.reset()
                continue
            return reply, err
        return None, "Claude Code wouldn't start."

    def _oneoff(self, msg, on_event, mode):
        deep, mcp = "deep" in mode, "mcp" in mode
        model = CFG.get("model_deep", "") if deep else CFG.get("model", "sonnet")
        on_event("mode", f"{model or 'default model'}{' (thinking hard)' if deep else ''}"
                         f"{', with connectors' if mcp else ''}")
        self.stop()                                  # the session moves to this run, then back
        cmd = self._base(model, mcp)
        cmd.insert(2, msg)
        if self.state.get("session") and self._fresh_session_ok():
            cmd += ["--resume", self.state["session"]]
        self.oneoff, q = self._spawn(cmd, subprocess.DEVNULL)
        try:
            reply, err, _ = self._collect(q, self.oneoff, on_event)
        finally:
            self.oneoff = None
        return reply, err

    def _collect(self, q, proc, on_event):
        deadline = time.time() + int(CFG.get("task_timeout_min", 30)) * 60
        while True:
            try:
                ev = q.get(timeout=1)
            except queue.Empty:
                if time.time() > deadline:
                    self._kill(proc)
                    return None, "That ran past the time limit, so I stopped it.", True
                continue
            if ev is None:
                time.sleep(0.2)
                err = "".join(getattr(proc, "err_lines", [])).strip() or "Claude Code stopped unexpectedly."
                return None, err[-400:], True
            kind = self._route(ev, on_event)
            if kind == "result":
                self.state["session_at"] = time.time()
                self.state["session_day"] = datetime.date.today().isoformat()
                save_state(self.state)
                if ev.get("result") and not ev.get("is_error"):
                    return ev["result"], None, False
                return None, (ev.get("result") or "No reply.")[-400:], False

    def drain(self, on_event, stop):
        """Between requests: keep reading the session so background helpers' boxes stay live,
        and so a late follow-up never gets mistaken for the answer to the next question."""
        q = self.events
        while q is not None and not stop.is_set():
            try:
                ev = q.get(timeout=0.3)
            except queue.Empty:
                continue
            if ev is None:
                return
            self._route(ev, on_event)

    def _route(self, ev, on_event):
        """Pass one stream event on as status updates. Returns the event type."""
        if ev.get("session_id"):
            self.state["session"] = ev["session_id"]
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "task_notification":     # a background helper finished
            on_event("agent_end", (ev.get("tool_use_id"), ev.get("status") not in ("completed", None)))
            return kind
        if True:
            content = (ev.get("message") or {}).get("content") or []
            parent = ev.get("parent_tool_use_id")      # set when a helper agent produced this event
            if kind == "assistant" and isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    t = c.get("type")
                    if t == "text" and (c.get("text") or "").strip():
                        if parent:
                            on_event("agent", (parent, "say", c["text"].strip()))
                        else:
                            on_event("text", c["text"].strip())
                    elif t == "thinking" and (c.get("thinking") or "").strip():
                        if parent:
                            on_event("agent", (parent, "think", c["thinking"].strip()))
                        else:
                            on_event("thought", c["thinking"].strip())
                    elif t == "tool_use":
                        name, inp = c.get("name", ""), c.get("input") or {}
                        if name in ("Task", "Agent"):
                            on_event("agent_start", (c.get("id"), inp.get("description") or inp.get("subagent_type")
                                                     or "Helper"))
                        if parent:
                            on_event("agent", (parent, "step", describe_tool(name, inp)))
                        else:
                            on_event("tool", describe_tool(name, inp))
            elif kind == "user" and isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        body = json.dumps(c.get("content", ""))
                        if "BLOCKED until the user confirms" in body:
                            on_event("blocked", "")
                        if "Async agent launched" not in body:     # a background helper isn't done yet
                            on_event("agent_end", (c.get("tool_use_id"), bool(c.get("is_error"))))
        return kind


# ---------------------------------------------------------------- memory (every word, on disk)
MEM = HOME / "memory"

TOPICS = {
    "todo.md": "# To-do\n\n",
    "week_summary.md": "# Last 7 days\n\n",
    "technical.md": "# Technical notes\n\n",
    "projects.md": "# Projects\n\n",
    "decisions.md": "# Decisions\n\n",
    "people.md": "# People\n\n",
    "history.md": "# History\n\n",
    "how_you_talk.md": ("# How you talk\n"
                        "Learned from your own words in the daily logs. Updated automatically while you use it "
                        "(see learn_every_min in config.yaml) and at end of day. Starts empty.\n\n"
                        "## Vocabulary\n\n"
                        "## Slang and shortenings\n\n"
                        "## Phrasing habits\n\n"
                        "## When you're annoyed / when you're pleased\n\n"
                        "## Your names for things\n\n"),
}


def learned_vocabulary():
    """Words from the 'Vocabulary' section of the profile, fed to the speech recogniser as hints."""
    try:
        text = (MEM / "topics" / "how_you_talk.md").read_text(encoding="utf-8")
        m = re.search(r"## Vocabulary\n(.*?)(?:\n## |\Z)", text, re.S)
        words = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        return words[:600]
    except OSError:
        return ""


class Memory:
    def __init__(self):
        (MEM / "days").mkdir(parents=True, exist_ok=True)
        (MEM / "topics").mkdir(exist_ok=True)
        for name, head in TOPICS.items():                 # topics/ is git-ignored; seeded from memory/templates/
            f = MEM / "topics" / name
            if not f.exists():
                tpl = MEM / "templates" / name
                f.write_text(tpl.read_text(encoding="utf-8") if tpl.exists() else head, encoding="utf-8")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(MEM / "memory.db"), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY, day TEXT, ts TEXT, who TEXT, text TEXT)")
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(text, content='turns', content_rowid='id')")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()
        self.day = None
        self._roll()

    def _roll(self):
        d = datetime.date.today().isoformat()
        if d == self.day:
            return
        self.day = d
        f = MEM / "days" / f"{d}.md"
        if not f.exists():
            f.write_text(f"# {datetime.date.today():%A %d %B %Y}\n\n", encoding="utf-8")
        (MEM / "today.md").write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        self._write_recent()

    def _write_recent(self):
        # start-up memory loads this small file, not the whole of today.md (keeps context rot down)
        try:
            lines = (MEM / "today.md").read_text(encoding="utf-8").splitlines()
            tail = lines[-int(CFG.get("recent_lines", 40)):]
            body = "\n".join(tail)
            cap = int(CFG.get("recent_chars", 9000))
            if len(body) > cap:
                body = body[-cap:]
            (MEM / "recent.md").write_text(
                "# Recent chat (last few lines only; full day is in memory/today.md, search older with search.py)\n"
                + body + "\n",
                encoding="utf-8")
        except OSError:
            pass

    def add(self, who, text):
        text = (text or "").strip()
        if not text:
            return
        try:
            with self.lock:
                self._roll()
                ts = now()
                cur = self.db.execute("INSERT INTO turns (day, ts, who, text) VALUES (?, ?, ?, ?)",
                                      (self.day, ts, who, text))
                if self.fts:
                    self.db.execute("INSERT INTO turns_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
                self.db.commit()
                line = f"[{ts}] {who}: {text}\n"
                for f in (MEM / "days" / f"{self.day}.md", MEM / "today.md"):
                    with open(f, "a", encoding="utf-8") as h:
                        h.write(line)
                self._write_recent()
        except Exception as e:
            log("memory", f"Couldn't save: {e}")


# ---------------------------------------------------------------- instant commands (no Claude)
ALIASES = {
    "vscode": "vs code", "visual studio code": "vs code", "vs code editor": "vs code",
    "google chrome": "chrome", "the browser": "browser", "a browser": "browser", "web browser": "browser",
    "my projects folder": "projects", "projects folder": "projects", "my projects": "projects",
    "file manager": "file explorer", "windows explorer": "file explorer", "the calculator": "calculator",
}


def _norm(text):
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9.' /-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    t = re.sub(r"^(?:(?:ok|okay|right|tars|hey tars|please|can you|could you|would you|will you)\s+)+", "", t)
    t = re.sub(r"\s+(?:please|for me|now|thanks|thank you)$", "", t)
    return t


def _domain(target):
    d = re.sub(r"\s+dot\s+", ".", target)
    d = d.replace(" ", "")
    if re.fullmatch(r"(https?://)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", d):
        return d if d.startswith("http") else "https://" + d
    return None


def instant_command(text):
    """Return (description, powershell command) for a plain open/go-to request, or None."""
    t = _norm(text)
    apps = {k.lower(): v for k, v in (CFG.get("apps") or {}).items()}
    sites = {k.lower(): v for k, v in (CFG.get("sites") or {}).items()}
    m = re.fullmatch(r"(?:search|search google|google search) for (.+)", t)
    if m:
        q = m.group(1).strip()
        url = str(CFG.get("search_url", "https://www.google.com/search?q=")) + re.sub(r"\s+", "+", q)
        return f"Searched Google for {q}", f'Start-Process "{url}"'
    m = re.fullmatch(r"(?:open|launch|start|load|bring up|fire up|run)(?: up)? (?:the |my |a )?(.+)", t) \
        or re.fullmatch(r"(?:go to|goto|take me to|browse to|visit|navigate to) (?:the )?(.+)", t)
    if not m:
        return None
    target = m.group(1).strip()
    target = re.sub(r"\s+(?:app|application|website|site|page)$", "", target)
    target = ALIASES.get(target, target)
    if target in apps:
        return f"Opened {target}", apps[target]
    if target in sites:
        return f"Opened {target}", f'Start-Process "{sites[target]}"'
    url = _domain(target)
    if url:
        return f"Opened {url}", f'Start-Process "{url}"'
    return None


def run_instant(cmd):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd], creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def chime():
    try:
        import winsound
        winsound.Beep(988, 60)
        winsound.Beep(1319, 80)
    except Exception:
        pass


def short_path(p):
    p = str(p or "").replace("\\", "/")
    base = str(CFG.get("projects_dir") or HERE.parent).replace("\\", "/").rstrip("/")
    if base and p.lower().startswith(base.lower() + "/"):
        return p[len(base) + 1:]
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[-2:]) if parts else p


def clip(text, n=150):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def describe_tool(name, inp):
    """One plain line for what Claude is doing right now."""
    if name in ("Bash", "PowerShell"):
        return "Running: " + clip(str(inp.get("command", "")).splitlines()[0] if inp.get("command") else "", 140)
    if name == "Write":
        return "Creating file: " + short_path(inp.get("file_path"))
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "Editing: " + short_path(inp.get("file_path") or inp.get("notebook_path"))
    if name == "Read":
        return "Reading: " + short_path(inp.get("file_path"))
    if name in ("Glob", "Grep", "LS"):
        return "Looking through files: " + clip(inp.get("pattern") or inp.get("path") or "", 80)
    if name == "WebSearch":
        return "Searching the web: " + clip(inp.get("query"), 100)
    if name == "WebFetch":
        return "Reading a web page: " + clip(inp.get("url"), 100)
    if name == "TodoWrite":
        return "Planning the steps"
    if name in ("Task", "Agent"):
        return "Starting a helper: " + clip(inp.get("description"), 100)
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"Using {parts[1] if len(parts) > 1 else 'a tool'}: {parts[-1].replace('_', ' ')}"
    return "Using " + name


# ---------------------------------------------------------------- status boxes
class Pane:
    """One small always-on-top box: status dot, title, timer and a word-wrapped feed.
    Drag anywhere to move it, drag the corner grip to resize, never takes focus."""
    BG, FG, SUB, DIM = "#111418", "#E8EBEF", "#AAB3BE", "#6F7883"
    COLOURS = {"Idle": "#6B7280", "Listening": "#E5484D", "Thinking": "#F5A524", "Working": "#3E82F7",
               "Speaking": "#30A46C", "Confirm": "#A07CF8", "Problem": "#E5484D",
               "Done": "#30A46C", "Failed": "#E5484D", "Stopped": "#6B7280"}

    def __init__(self, win, scale, size, pos, on_moved=None, on_resized=None, on_double=None):
        import tkinter as tk
        self.win, self.s = win, scale
        self.on_moved, self.on_resized, self.on_double = on_moved, on_resized, on_double
        self.min_w, self.min_h = int(200 * scale), int(44 * scale)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=self.BG)                     # the window colour is the border
        edge = max(2, int(3 * scale))
        frame = tk.Frame(win, bg=self.BG, padx=int(12 * scale), pady=int(8 * scale))
        frame.pack(fill="both", expand=True, padx=edge, pady=edge)
        self.top = top = tk.Frame(frame, bg=self.BG)
        top.pack(fill="x")
        d = int(12 * scale)
        self.dot = tk.Canvas(top, width=d, height=d, bg=self.BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, int(8 * scale)))
        self.dot_id = self.dot.create_oval(1, 1, d - 1, d - 1, fill=self.COLOURS["Idle"], outline="")
        self.clock = tk.Label(top, text="", fg=self.SUB, bg=self.BG, font=("Consolas", 10))
        self.clock.pack(side="right")
        self.title = tk.Label(top, text="", fg=self.FG, bg=self.BG, font=("Segoe UI Semibold", 11), anchor="w")
        self.title.pack(side="left", fill="x", expand=True)
        self.text = tk.Text(frame, bg=self.BG, fg=self.SUB, font=("Segoe UI", 9), wrap="word", relief="flat",
                            highlightthickness=0, borderwidth=0, height=1, cursor="fleur", padx=0, pady=0)
        self.text.tag_configure("think", foreground=self.DIM, font=("Segoe UI", 9, "italic"))
        self.text.tag_configure("say", foreground=self.FG)
        self.text.tag_configure("step", foreground=self.SUB)
        self.text.tag_configure("now", foreground=self.FG)
        self.text.pack(fill="both", expand=True, pady=(int(4 * scale), 0))
        self.text.configure(state="disabled")
        self.grip = tk.Label(win, text="\u25E2", fg="#4A515A", bg=self.BG, font=("Segoe UI", 9), cursor="size_nw_se")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<ButtonPress-1>", self._grip_start)
        self.grip.bind("<B1-Motion>", self._grip_move)
        self.grip.bind("<ButtonRelease-1>", self._grip_end)
        self._drag = None
        for w in (win, frame, top, self.dot, self.title, self.clock, self.text):
            w.bind("<ButtonPress-1>", self._grab)
            w.bind("<B1-Motion>", self._move)
            w.bind("<ButtonRelease-1>", self._drop)
            w.bind("<Double-Button-1>", lambda e: self.on_double and self.on_double())
            if w is not self.text:
                w.configure(cursor="fleur")
        self.w, self.h = max(self.min_w, int(size[0])), max(self.min_h, int(size[1]))
        self.win.geometry(f"{self.w}x{self.h}+{int(pos[0])}+{int(pos[1])}")
        self.shown = None
        win.update_idletasks()
        self.no_focus()

    def no_focus(self):
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.win.winfo_id())
            ex = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x00000080)   # NOACTIVATE | TOOLWINDOW
        except Exception:
            pass

    # -- move
    def _grab(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
        return "break"

    def _move(self, e):
        if self._drag:
            self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")
        return "break"

    def _drop(self, e):
        if self._drag:
            self._drag = None
            if self.on_moved:
                self.on_moved(self.win.winfo_x(), self.win.winfo_y())
        return "break"

    # -- resize
    def _grip_start(self, e):
        self._sizing = (e.x_root, e.y_root, self.w, self.h)

    def _grip_move(self, e):
        x0, y0, w0, h0 = self._sizing
        self.w = max(self.min_w, w0 + e.x_root - x0)
        self.h = max(self.min_h, h0 + e.y_root - y0)
        self.win.geometry(f"{self.w}x{self.h}")

    def _grip_end(self, e):
        if self.on_resized:
            self.on_resized(self.w, self.h)

    # -- content
    def show(self, state, title, lines, elapsed, colour=None):
        """lines: list of (text, kind) with kind think / say / step; the newest is highlighted."""
        colour = colour or self.COLOURS.get(state, self.COLOURS["Idle"])
        view = (state, title, tuple(lines))
        if view != self.shown:
            self.shown = view
            self.title.config(text=clip(title, 70))
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            for i, (line, kind) in enumerate(lines):
                tag = "now" if i == len(lines) - 1 and kind != "think" else kind
                self.text.insert("end", ("\n" if i else "") + line, tag)
            self.text.configure(state="disabled")
            self.text.see("end")
            self.dot.itemconfig(self.dot_id, fill=colour)
        self.clock.config(text=f"{int(elapsed // 60)}:{int(elapsed % 60):02d}" if elapsed is not None else "")
        return colour

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class Overlay:
    """The main status box, plus one box per helper agent stacked underneath it."""
    TITLES = {"Confirm": "Waiting for you"}
    MAX_AGENT_BOXES = 6

    def __init__(self, app):
        import tkinter as tk
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.tk, self.app = tk, app
        r = self.root = tk.Tk()
        r.withdraw()
        s = self.s = r.winfo_fpixels("1i") / 96.0
        self.margin = int(16 * s)
        size = app.state.get("overlay_size") or [int(360 * s), int(110 * s)]
        pos = app.state.get("overlay_pos") or self._dock(size)
        self.main = Pane(r, s, size, pos, on_moved=self._moved, on_resized=self._resized, on_double=self._reset)
        r.deiconify()
        self.main.no_focus()
        # one small numbered square per helper in the top bar: click to show / hide its box
        self.icons = tk.Frame(self.main.top, bg=Pane.BG)
        self.icons.pack(side="right", padx=(0, int(8 * s)))
        self.icon_view = None
        self.panes = {}                  # agent id -> Pane
        self.n, self.blink = 0, False
        signal.signal(signal.SIGINT, lambda *a: r.after(0, r.destroy))
        self.tick()

    def _dock(self, size):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        where = str(CFG.get("overlay_position", "top-right")).lower()
        x = self.margin if "left" in where else sw - int(size[0]) - self.margin
        # clear of window title bars (minimise / maximise / close) when docked at the top
        y = sh - int(size[1]) - self.margin * 4 if "bottom" in where else int(self.margin * 4.5)
        return [x, y]

    def _moved(self, x, y):
        self.app.state["overlay_pos"] = [x, y]
        save_state(self.app.state)
        self._restack()

    def _resized(self, w, h):
        self.app.state["overlay_size"] = [w, h]
        save_state(self.app.state)
        self._restack()

    def _reset(self):
        self.app.state.pop("overlay_pos", None)
        save_state(self.app.state)
        x, y = self._dock([self.main.w, self.main.h])
        self.root.geometry(f"+{x}+{y}")
        self._restack()

    def _slot(self, i):
        """Where agent box number i goes: under the main box, in a column."""
        gap = int(6 * self.s)
        x, y = self.root.winfo_x(), self.root.winfo_y() + self.main.h + gap
        for p in self._visible()[:i]:
            y += p.h + gap
        return [x, y]

    def _visible(self):
        return [p for aid, p in self.panes.items() if not self.app.agents.get(aid, {}).get("hidden")]

    def _restack(self):
        for i, p in enumerate(self._visible()):
            if not getattr(p, "moved", False):
                x, y = self._slot(i)
                p.win.geometry(f"+{x}+{y}")

    def _set_hidden(self, aid, hidden):
        a, p = self.app.agents.get(aid), self.panes.get(aid)
        if not a or not p:
            return
        a["hidden"] = hidden
        if hidden:
            p.win.withdraw()
        else:
            p.win.deiconify()
            p.win.attributes("-topmost", True)
            p.no_focus()
        self._restack()

    def _toggle(self, aid):
        a = self.app.agents.get(aid)
        if a:
            self._set_hidden(aid, not a.get("hidden"))
        return "break"

    def _sync_icons(self):
        items = list(self.app.agents.items())
        view = tuple((aid, a["status"], bool(a.get("hidden"))) for aid, a in items)
        if view == self.icon_view:
            return
        self.icon_view = view
        for w in self.icons.winfo_children():
            w.destroy()
        for n, (aid, a) in enumerate(items, start=1):
            colour = Pane.COLOURS.get(a["status"], Pane.COLOURS["Idle"])
            hidden = a.get("hidden")
            lbl = self.tk.Label(self.icons, text=str(n), font=("Segoe UI Semibold", 8), width=2, cursor="hand2",
                                bg="#2A2F36" if hidden else colour, fg=colour if hidden else Pane.BG)
            lbl.pack(side="left", padx=(int(3 * self.s), 0))
            lbl.bind("<Button-1>", lambda e, aid=aid: self._toggle(aid))

    def _main_lines(self, state, detail):
        if state in ("Working", "Thinking") and self.app.feed:
            return list(self.app.feed)[-40:]
        return [(detail, "say")] if detail else []

    def _sync_agents(self):
        agents = self.app.agents
        for aid in [a for a in self.panes if a not in agents]:    # cleared by a new job or cancel
            self.panes.pop(aid).destroy()
            self._restack()
        for n, (aid, a) in enumerate(list(agents.items()), start=1):
            if aid not in self.panes:
                if len(self._visible()) >= self.MAX_AGENT_BOXES:
                    a["hidden"] = True                # too many on screen: starts hidden, click its square
                win = self.tk.Toplevel(self.root)
                pane = Pane(win, self.s, [self.main.w, int(130 * self.s)], self._slot(len(self._visible())),
                            on_double=lambda aid=aid: self._set_hidden(aid, True))
                pane.on_moved = lambda x, y, pane=pane: setattr(pane, "moved", True)
                self.panes[aid] = pane
                if a.get("hidden"):
                    win.withdraw()
            pane = self.panes.get(aid)
            if pane:
                end = a["ended"] or time.time()
                lines = list(a["feed"])[-40:] or [("Starting", "step")]
                colour = pane.show(a["status"], f"{n} \u00b7 {a['title']}", lines, end - a["started"])
                if a["status"] == "Working" and self.blink:
                    pane.dot.itemconfig(pane.dot_id, fill="#2A2F36")
                else:
                    pane.dot.itemconfig(pane.dot_id, fill=colour)

    def tick(self):
        try:
            self.n += 1
            if self.n % 3 == 0:
                self.blink = not self.blink
            state, detail, elapsed = self.app.status()
            title = Overlay.TITLES.get(state, state)
            colour = self.main.show(state, title, self._main_lines(state, detail), elapsed)
            self.root.attributes("-alpha", 0.75 if state == "Idle" else 0.95)
            if state == "Listening":
                # bright red while his voice is coming in, dim red while the mic is open but quiet
                hearing = detail == "Hearing you"
                self.main.dot.itemconfig(self.main.dot_id, fill=colour if hearing else "#7A2A2C")
                self.root.configure(bg=colour if hearing else "#3A1D1F")
            else:
                pulse = state in ("Working", "Thinking") and self.blink
                self.main.dot.itemconfig(self.main.dot_id, fill="#2A2F36" if pulse else colour)
                self.root.configure(bg=Pane.BG)
            self._sync_agents()
            self._sync_icons()
            if self.n % 10 == 0:
                self.root.attributes("-topmost", True)
                for p in self.panes.values():
                    p.win.attributes("-topmost", True)
        except Exception as e:
            if self.n % 50 == 0:
                log("overlay", f"Status box error: {e}")
        self.root.after(120, self.tick)


# ---------------------------------------------------------------- the app
CONFIRM_RE = re.compile(r"^(yes,? )?(confirmed?|confirm it|go ahead|do it|yes do it|send it|delete it|approved?)\b", re.I)
ACKS = ["Give me a sec.", "Bear with me.", "This one's taking a minute.", "Working on it."]
START_RE = re.compile(r"(?:initiate|initialise|initialize|start|wake up|morning)\s+(?:tars|tarz|taz|stars|tars mode)|start of (?:the )?day")
END_RE = re.compile(r"(?:initiate\s+)?end of (?:the )?day|shut ?down for the day")
LEARN_PROMPT = ("Update the user's speech profile in memory/topics/how_you_talk.md (read it first; edit it in place and "
                "change nothing else). Below are their NEW spoken lines (transcribed) since the last update. Merge what "
                "they show: slang and shortenings with what they mean by each; phrasing habits "
                "(how they start, ask, end, sentence length); how they sound annoyed vs pleased; their names for apps, "
                "projects and people. Keep only patterns seen at least twice across the profile and these lines, or "
                "ones they say are theirs. Ignore obvious speech-recognition mistakes. Keep up to 60 of their distinctive "
                "words and names, comma-separated, under '## Vocabulary'. Keep the file under about 120 lines, facts "
                "not prose, no customer, patient or other sensitive data. Add or update a 'Last learned' line with "
                "today's date and time. Reply with just: ok\n\nNEW LINES:\n{lines}")
START_PROMPT = ("Start of day (the user said 'initiate TARS'). Read memory/topics/week_summary.md and memory/topics/todo.md. "
                "Brief them out loud in under 40 seconds: the few things from the past week worth remembering, "
                "then today's to-dos, most important first. If both are empty, say so in one line and ask what's on today.")
END_PROMPT = ("End of day (the user said 'initiate end of day'). Go through today's full transcript in memory/days/{day}.md "
              "and file it: update memory/topics/technical.md, projects.md, decisions.md and people.md with anything "
              "new or changed; append a dated entry to history.md; update todo.md (mark done, add new, carry over the rest); "
              "add today's summary (about 10 lines, under a '## {day}' heading) to week_summary.md and remove days older "
              "than 7 days from it. Also update memory/topics/how_you_talk.md from today's lines spoken by the user (their words, slang, "
              "phrasing; patterns seen at least twice; Vocabulary list). "
              "Keep every file tight, no customer, patient or other sensitive data. Then tell them in two short "
              "sentences what you filed and what tops tomorrow's list.")


class App:
    def __init__(self):
        self.state = load_state()
        self.mouth = Mouth()
        log("setup", f"Voice: {self.mouth.voice_name}")
        log("setup", "Loading speech recognition (first run downloads the model)...")
        self.ears = Ears()
        log("setup", f"Microphone: {self.ears.mic_name} | speech recognition on {self.ears.where}")
        self.brain = Brain(self.state)
        self.memory = Memory()
        self.unseen = []        # things that happened without Claude (instant commands etc.), passed on next time
        self.recording = False
        self.working = False
        self.transcribing = False
        self.detail = ""
        self.task_started = 0.0
        self.awaiting_confirm = False
        self.error_msg = ""
        self.error_until = 0.0
        self.last_reply = ""
        self.last_press = 0.0
        self.feed = collections.deque(maxlen=80)     # main box history: (text, kind)
        self.agents = {}                              # helper agent id -> its box's data
        self.lock = threading.Lock()
        write_hook_settings()

    # -- what the status box shows
    def status(self):
        if self.recording:
            talking = self.ears.level > float(CFG.get("voice_threshold", 0.015))
            return ("Listening", "Hearing you" if talking else "Listening\u2026 press ` again to send", None)
        if self.transcribing:
            return "Thinking", "Working out what you said", None
        if self.working:
            return "Working", self.detail or "Starting", time.time() - self.task_started
        if self.mouth.speaking:
            return "Speaking", self.last_reply or "", None
        if self.awaiting_confirm:
            return "Confirm", "Say 'confirmed' to go ahead, or tell it not to.", None
        if time.time() < self.error_until:
            return "Problem", self.error_msg, None
        return "Idle", "Press ` to talk", None

    # -- hotkeys
    def on_toggle(self):
        t = time.time()
        if t - self.last_press < 0.35:        # ignore key repeat
            return
        self.last_press = t
        with self.lock:
            if self.recording:
                self.recording = False
                audio = self.ears.stop()
                beep("stop")
                threading.Thread(target=self.handle_audio, args=(audio,), daemon=True).start()
            else:
                self.mouth.stop()              # talking over it shuts it up
                try:
                    self.ears.start()
                except Exception as e:
                    log("error", f"Microphone failed: {e}")
                    beep("error")
                    return
                self.recording = True
                beep("start")

    def on_cancel(self):
        self.agents.clear()                    # closes the helper boxes
        self.mouth.stop()
        if self.brain.cancel():
            self.mouth.say("Cancelled.")

    # -- one utterance
    def handle_audio(self, audio):
        if len(audio) < 16000 * 0.4:
            return
        self.transcribing = True
        try:
            text = self.ears.transcribe(audio)
        finally:
            self.transcribing = False
        if not text:
            self.mouth.say("Didn't catch that.")
            return
        log(USER, text)
        self.memory.add(USER, text)
        self.awaiting_confirm = False
        if self.local_command(text):
            return
        inst = instant_command(text)
        if inst:
            desc, cmd = inst
            run_instant(cmd)
            chime()
            log("instant", desc)
            self.memory.add("TARS", f"(instant) {desc}")
            self.unseen.append(f"{now()} {USER} said: \"{text}\" -> the program did it instantly, without you: {desc}")
            return
        if self.working:
            self.mouth.say("Still on the last one. Say cancel if you want me to drop it.")
            return
        if CONFIRM_RE.match(text.strip()):
            TOKEN_FILE.write_text(str(time.time() + 180), encoding="utf-8")
            log("system", "Confirmation recorded for the next 3 minutes.")
        threading.Thread(target=self.do_task, args=(text,), daemon=True).start()

    def local_command(self, text):
        handled = self._local_command(text)
        if handled:
            self.unseen.append(f"{now()} {USER} said: \"{text}\" -> handled by the program as a voice command")
        return handled

    def _local_command(self, text):
        t = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
        if re.fullmatch(r"(ok |okay )?(stop|stop talking|shut up|quiet|be quiet|shush)", t):
            self.mouth.stop()
            return True
        if re.fullmatch(r"(cancel|cancel that|abort|stop the task|stop working)", t):
            self.on_cancel()
            return True
        if re.fullmatch(r"(repeat|repeat that|say that again|what did you say)", t):
            self.mouth.say(self.last_reply or "Nothing to repeat yet.")
            return True
        if re.fullmatch(r"(new session|start fresh|fresh start|new conversation|clear the conversation)", t):
            self.brain.reset()
            chime()
            return True
        if len(t.split()) <= 7 and START_RE.search(t):
            self.brain.reset()
            threading.Thread(target=self.do_task, args=(text, START_PROMPT), daemon=True).start()
            return True
        if len(t.split()) <= 7 and END_RE.search(t):
            day = datetime.date.today().isoformat()
            threading.Thread(target=self.do_task, args=(text, END_PROMPT.format(day=day), "deep"),
                             daemon=True).start()
            return True
        m = re.search(r"(humou?r|honesty|mirror) (?:setting |level )?(?:to |at )?(\d{1,3})", t)
        if m and len(t.split()) <= 8:
            which = {"hon": "honesty", "mir": "mirror"}.get(m.group(1)[:3], "humour")
            n = max(0, min(100, int(m.group(2))))
            self.state[which] = n
            save_state(self.state)
            extra = ""
            if which == "humour" and n >= 90:
                extra = " God help you."
            elif which == "honesty" and n < 70:
                extra = " I'll be appropriately economical with the truth."
            elif which == "mirror" and n >= 80:
                extra = " Noted. I will try to sound like you."
            self.mouth.say(f"{which.capitalize()} {n} percent.{extra}")
            return True
        return False

    def _mode(self, text):
        low = text.lower()
        mode = ""
        if any(p in low for p in CFG.get("deep_phrases", ["think hard"])):
            mode += "deep"
        if any(re.search(r"\b" + re.escape(k), low) for k in CFG.get("mcp_keywords", [])):
            mode += "mcp"
        return mode or "normal"

    def _agent_event(self, kind, info, steps=None):
        """Helper agent updates, for its own status box."""
        if kind == "agent_start":
            aid, title = info
            if aid:
                self.agents[aid] = {"title": clip(title, 70), "status": "Working", "started": time.time(),
                                    "ended": None, "feed": collections.deque(maxlen=80)}
                self.feed.append((f"Helper started: {title}", "step"))
                log("agent", f"Started: {title}")
        elif kind == "agent":
            aid, sub, txt = info
            a = self.agents.get(aid)
            if a:
                a["feed"].append((clip(txt, 400), sub))
                if sub == "step":
                    if steps is not None:
                        steps.append(txt)
                    log("agent", f"{a['title']}: {txt}")
            else:
                self.feed.append((clip(txt, 400), sub))
        elif kind == "agent_end":
            aid, failed = info
            a = self.agents.get(aid)
            if a and a["status"] == "Working":
                a["status"], a["ended"] = ("Failed" if failed else "Done"), time.time()
                self.feed.append((f"Helper {a['status'].lower()}: {a['title']}", "step"))
                log("agent", f"{a['status']}: {a['title']}")

    def _between_event(self, kind, info):
        """Stream events that arrive after the reply (background helpers, late follow-ups): show and log.
        Spoken only when the job is finished: no helper still working and no [quiet] marker in the text."""
        if kind in ("agent_start", "agent", "agent_end"):
            self._agent_event(kind, info)
        elif kind == "text":
            self.feed.append((clip(info, 400), "say"))
            log("note", clip(info, 300))
            self.memory.add("TARS", f"(after the reply) {info}")
            busy = any(a["status"] == "Working" for a in self.agents.values())
            if self.working or busy or re.search(r"\[quiet\]", info, re.I):
                return
            if re.fullmatch(r"\s*\[done\]\s*", info, re.I):
                chime()
                return
            spoken = re.sub(r"\[(?:done|quiet)\]", "", info, flags=re.I).strip()
            if spoken:
                self.last_reply = for_speech(spoken)
                self.mouth.say(self.last_reply)
        elif kind == "tool":
            self.feed.append((info, "step"))
            log("step", info)

    def _start_drain(self):
        self.drain_stop = threading.Event()
        self.drain_thread = threading.Thread(target=self.brain.drain, args=(self._between_event, self.drain_stop),
                                             daemon=True)
        self.drain_thread.start()

    def _stop_drain(self):
        stop, t = getattr(self, "drain_stop", None), getattr(self, "drain_thread", None)
        if stop:
            stop.set()
        if t:
            t.join(2)

    def do_task(self, text, prompt=None, mode=None):
        if self.working:
            return
        self._stop_drain()                     # only one reader of the session at a time
        self.detail = "Starting"
        self.task_started = time.time()
        self.feed.clear()
        self.agents.clear()                    # last job's helper boxes close when a new job starts
        self.working = True
        done = threading.Event()
        steps = []
        notes = []

        def nudges():
            # only speak up if it's genuinely taking a while
            first = float(CFG.get("ack_after_s", 8))
            if done.wait(first):
                return
            if not self.mouth.speaking and not self.recording:
                self.mouth.say(random.choice(ACKS))
            waited = first
            while not done.wait(15):
                waited += 15
                if waited >= 120 and int(waited) % 180 < 15 and not self.mouth.speaking and not self.recording:
                    self.mouth.say("Still at it.")

        threading.Thread(target=nudges, daemon=True).start()

        def on_event(kind, info):
            if kind == "tool":
                self.detail = info
                steps.append(info)
                self.feed.append((info, "step"))
                log("step", info)
            elif kind == "text":
                self.detail = clip(info.splitlines()[0], 150)
                notes.append(info)
                self.feed.append((clip(info, 400), "say"))
            elif kind == "thought":
                self.feed.append((clip(info, 400), "think"))
            elif kind in ("agent_start", "agent", "agent_end"):
                self._agent_event(kind, info, steps)
            elif kind == "mode":
                log("system", "Using " + info)
            elif kind == "blocked":
                self.awaiting_confirm = True
                log("system", "Blocked by the safety check until you say 'confirmed'.")

        message = prompt or text
        if self.unseen:
            message = ("[Happened since your last reply, without you (already in today's log):\n"
                       + "\n".join(self.unseen[-20:]) + "]\n\n" + message)
            self.unseen.clear()
        run_mode = mode or self._mode(text)
        try:
            reply, err = self.brain.ask(message, on_event, run_mode)
        except Exception as e:
            reply, err = None, str(e)
        done.set()
        self.working = False
        if run_mode == "normal" and not self.brain.cancelled:
            self._start_drain()                # background helpers keep updating their boxes
        else:
            for a in list(self.agents.values()):   # one-off runs end here, so nothing more will arrive
                if a["status"] == "Working":
                    a["status"], a["ended"] = "Stopped", time.time()
        for n in notes:                       # running commentary, minus the final reply (logged below)
            if not reply or n.strip() != reply.strip():
                log("note", clip(n, 300))
        if steps:
            self.memory.add("steps", clip("; ".join(steps), 800))
        if self.brain.cancelled:
            self.awaiting_confirm = False
            return
        if reply:
            quiet = bool(re.fullmatch(r"\s*\[done\]\s*", reply, re.I))
            reply = re.sub(r"\[done\]", "", reply, flags=re.I).strip()
            if re.search(r"\bconfirm", reply, re.I):
                self.awaiting_confirm = True
            log("Claude", reply or "[done]")
            self.memory.add("TARS", reply or "(done, no reply needed)")
            if quiet or not reply:
                chime()
                return
            self.last_reply = for_speech(reply)
            self.mouth.say(self.last_reply)
        else:
            log("error", err or "no reply")
            self.memory.add("error", err or "no reply")
            beep("error")
            self.error_msg = clip(err or "No reply", 150)
            self.error_until = time.time() + 20
            if err and re.search(r"authenticat|log ?in|oauth", err, re.I):
                self.mouth.say("Claude Code has logged out. Open a terminal, run claude, and type slash login.")
            else:
                self.mouth.say("That failed. The details are in the window.")

    # -- OPT-IN: keep the learned memory (assistant/memory/topics) in your OWN GitHub repo (the `origin` remote)
    def _sync(self):
        """Off unless sync_memory: true. Commits assistant/memory/topics (force-added, as the folder is git-ignored),
        merges and pushes to `origin` ONLY. Refuses if origin is missing, disabled or points at the upstream repo.
        Never raises; on any problem it logs and leaves things as they were."""
        if not CFG.get("sync_memory", False) or not (HERE / ".git").exists():
            return
        topics = "assistant/memory/topics"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def git(*args):
            return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=180, creationflags=flags)

        def norm(u):
            u = u.strip().lower().rstrip("/")
            return u[:-4] if u.endswith(".git") else u
        try:
            o = git("remote", "get-url", "origin")
            if o.returncode != 0:
                log("sync", "memory sync is on but there is no 'origin' remote; skipped. Re-run install.ps1 to set one up.")
                return
            origin, push = o.stdout.strip(), git("remote", "get-url", "--push", "origin").stdout.strip()
            up = git("remote", "get-url", "upstream").stdout.strip()
            if not push or "DISABLED" in push.upper() or (up and norm(origin) == norm(up)):
                log("sync", "refusing to sync: origin is missing, disabled, or is the upstream repo.")
                return
            branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
            git("add", "-f", "--", topics)
            if git("diff", "--cached", "--quiet", "--", topics).returncode != 0:
                git("commit", "-m", "memory sync", "--", topics)
            r = git("pull", "--no-rebase", "--no-edit", "--autostash", "-X", "theirs", "origin", branch)
            if r.returncode != 0 and "couldn't find remote ref" not in (r.stderr or ""):
                git("merge", "--abort")
                log("sync", f"pull failed, skipping this round: {clip(r.stderr or r.stdout, 200)}")
                return
            r = git("push", "origin", branch)
            if r.returncode != 0:
                log("sync", f"push failed: {clip(r.stderr or r.stdout, 200)}")
            else:
                log("sync", "memory pushed to your own GitHub repo (origin)")
        except Exception as e:
            log("sync", f"failed: {e}")

    # -- automatic speech-profile learning (replaces the old "learn how I talk" voice command)
    def _restart_reminder_loop(self):
        every = float(CFG.get("restart_reminder_min", 0)) * 60
        if every <= 0:
            return
        while True:
            time.sleep(every)
            try:
                while self.mouth.speaking or self.recording:
                    time.sleep(5)
                mins = int(every // 60)
                self.mouth.say(f"Reminder: I've been running {mins} minutes. Restart the voice app when you get a chance, "
                               "it clears my memory.")
                log("reminder", "Restart the voice app to clear memory (every "
                    f"{mins} min).")
            except Exception:
                pass

    def _learn_loop(self):
        every = float(CFG.get("learn_every_min", 10)) * 60
        if every <= 0:
            return
        self._sync()
        time.sleep(60)
        while True:
            try:
                self._sync()                       # learn from an up-to-date profile (only if sync is on)
                self._learn_once()
                self._sync()
            except Exception as e:
                log("learn", f"failed: {e}")
            time.sleep(every)

    def _learn_once(self):
        day = datetime.date.today().isoformat()
        f = MEM / "days" / f"{day}.md"
        if not f.exists():
            return
        done = self.state.get("learn_upto") or {}
        last = done.get("t", "00:00:00") if done.get("day") == day else "00:00:00"
        new = []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\[(\d\d:\d\d:\d\d)\] " + re.escape(USER) + r": (.+)", line)
            if m and m.group(1) > last:
                new.append((m.group(1), m.group(2).strip()))
        if len(new) < int(CFG.get("learn_min_lines", 3)):
            return
        prompt = LEARN_PROMPT.format(lines="\n".join(f"- {t}" for _, t in new))
        cmd = [self.brain.claude, "-p", prompt, "--model", CFG.get("model", "sonnet"),
               "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Edit", "Write", "--strict-mcp-config"]
        r = subprocess.run(cmd, cwd=str(HOME), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log("learn", f"failed: {clip(r.stderr or r.stdout, 200)}")
            return
        self.state["learn_upto"] = {"day": day, "t": new[-1][0]}
        save_state(self.state)
        log("learn", f"speech profile updated from {len(new)} new lines")

    def run(self):
        import keyboard
        hot = CFG.get("hotkey", "`")
        keyboard.add_hotkey(hot, self.on_toggle, suppress=True)
        keyboard.add_hotkey(CFG.get("cancel_hotkey", "ctrl+`"), self.on_cancel, suppress=True)
        log("ready", f"Press {hot} to talk, {hot} again to send. Ctrl+C in this window quits.")
        self.mouth.say("Online.")
        threading.Thread(target=self._learn_loop, daemon=True).start()
        threading.Thread(target=self._restart_reminder_loop, daemon=True).start()
        self.memory.add("system", "Assistant started")
        if CFG.get("overlay", True):
            try:
                self.overlay = Overlay(self)
            except Exception as e:
                log("setup", f"Status box unavailable ({e}), console only.")
            else:
                self.overlay.root.mainloop()
                log("system", "Shutting down.")
                return
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("system", "Shutting down.")


def write_hook_settings():
    """Point Claude Code's safety hook at guard.py, wherever this folder lives."""
    py = pathlib.Path(sys.executable).as_posix()
    guard = (HOME / "guard.py").as_posix()
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": f'"{py}" "{guard}"'}]}
            ]
        }
    }
    d = HOME / ".claude"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


if __name__ == "__main__":
    App().run()
