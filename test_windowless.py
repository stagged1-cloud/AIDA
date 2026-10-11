"""Tests for the windowless (pythonw, no console) start path. Run: .venv\\Scripts\\python.exe -m unittest test_windowless -v"""
import os
import re
import sys
import tempfile
import pathlib
import unittest
import subprocess
from types import SimpleNamespace
from unittest import mock

import windowless

HERE = pathlib.Path(__file__).resolve().parent


def fake_sys(stdout=None, stderr=None):
    return SimpleNamespace(stdout=stdout, stderr=stderr)


class RedirectTests(unittest.TestCase):
    def test_none_streams_are_redirected_to_file(self):
        # pythonw gives sys.stdout / sys.stderr = None: both must end up writing to logs\console.txt
        with tempfile.TemporaryDirectory() as d:
            s = fake_sys()
            self.assertTrue(windowless.setup(d, mod=s))
            print("hello", file=s.stdout)
            s.stderr.write("oops\n")
            s.stdout.flush()
            text = (pathlib.Path(d) / "console.txt").read_text(encoding="utf-8")
            self.assertIn("hello", text)
            self.assertIn("oops", text)
            self.assertIn("windowless", text)
            s.stdout.f.close()

    def test_real_streams_left_alone(self):
        # a normal console start must not be touched
        a, b = object(), object()
        s = fake_sys(a, b)
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(windowless.setup(d, mod=s))
            self.assertFalse((pathlib.Path(d) / "console.txt").exists())
        self.assertIs(s.stdout, a)
        self.assertIs(s.stderr, b)

    def test_only_missing_stream_replaced(self):
        keep = object()
        s = fake_sys(stdout=keep, stderr=None)
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(windowless.setup(d, mod=s))
            self.assertIs(s.stdout, keep)
            self.assertIsInstance(s.stderr, windowless.RotatingFile)
            s.stderr.f.close()

    def test_rotation_keeps_limited_files(self):
        # edge: the file must roll over instead of growing forever, and keep only KEEP files
        with tempfile.TemporaryDirectory() as d:
            r = windowless.RotatingFile(os.path.join(d, "console.txt"), max_bytes=200, keep=3)
            for i in range(60):
                r.write(f"line {i:03d} " + "x" * 20 + "\n")
            r.f.close()
            names = sorted(os.listdir(d))
            self.assertEqual(names, ["console.txt", "console.txt.1", "console.txt.2"])

    def test_write_never_raises_on_bad_input_or_closed_file(self):
        with tempfile.TemporaryDirectory() as d:
            r = windowless.RotatingFile(os.path.join(d, "c.txt"))
            r.f.close()
            self.assertEqual(r.write("after close"), 11)      # swallowed
            self.assertEqual(r.write(""), 0)
            r.flush()
            self.assertFalse(r.isatty())

    def test_unwritable_log_dir_does_not_crash(self):
        # invalid input: log path under a file, not a directory
        with tempfile.NamedTemporaryFile() as f:
            r = windowless.RotatingFile(os.path.join(f.name, "console.txt"))
            self.assertIsNone(r.f)
            self.assertEqual(r.write("x"), 1)


class FatalTests(unittest.TestCase):
    def test_message_box_called_with_error_and_log_path(self):
        box = mock.Mock()
        text = "Traceback (most recent call last):\n  File x\nValueError: bad config\n"
        body = windowless.show_fatal(text, r"C:\app\logs\console.txt", msgbox=box)
        box.assert_called_once()
        self.assertIn("ValueError: bad config", box.call_args[0][0])
        self.assertIn(r"C:\app\logs\console.txt", box.call_args[0][0])
        self.assertEqual(body, box.call_args[0][0])

    def test_message_box_failure_is_swallowed(self):
        windowless.show_fatal("RuntimeError: x", "log", msgbox=mock.Mock(side_effect=OSError("no ui")))

    def test_empty_error_text(self):
        box = mock.Mock()
        windowless.show_fatal("", "log", msgbox=box)
        self.assertIn("unknown error", box.call_args[0][0])

    def test_default_msgbox_uses_ctypes_user32(self):
        fake_user32 = mock.Mock()
        with mock.patch("ctypes.windll", SimpleNamespace(user32=fake_user32), create=True):
            windowless._default_msgbox("body", "title")
        fake_user32.MessageBoxW.assert_called_once()
        self.assertEqual(fake_user32.MessageBoxW.call_args[0][1:3], ("body", "title"))

    def test_excepthook_windowless_shows_box_and_writes_traceback(self):
        box = mock.Mock()
        err = windowless.RotatingFile.__new__(windowless.RotatingFile)
        written = []
        err = SimpleNamespace(write=written.append, flush=lambda: None)
        old_hook, old_err = sys.excepthook, sys.stderr
        try:
            sys.stderr = err
            hook = windowless.install_excepthook("logs", True, msgbox=box)
            try:
                raise KeyError("boom")
            except KeyError:
                hook(*sys.exc_info())
        finally:
            sys.excepthook, sys.stderr = old_hook, old_err
        self.assertIn("KeyError", "".join(written))
        box.assert_called_once()

    def test_excepthook_console_mode_no_box_and_ctrl_c_quiet(self):
        box = mock.Mock()
        old_hook, old_err = sys.excepthook, sys.stderr
        try:
            sys.stderr = SimpleNamespace(write=lambda s: None, flush=lambda: None)
            hook = windowless.install_excepthook("logs", False, msgbox=box)       # console: traceback is visible
            hook(RuntimeError, RuntimeError("x"), None)
            hook2 = windowless.install_excepthook("logs", True, msgbox=box)       # windowless but Ctrl+C-ish
            hook2(KeyboardInterrupt, KeyboardInterrupt(), None)
        finally:
            sys.excepthook, sys.stderr = old_hook, old_err
        box.assert_not_called()


class ConfigCheckTests(unittest.TestCase):
    """The helper start.bat calls: windowless.py check -> exit 0 only when chat_window is explicitly true."""

    def check(self, content):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.yaml")
            if content is not None:
                pathlib.Path(p).write_text(content, encoding="utf-8")
            return windowless.chat_window_enabled(p)

    def test_true(self):
        self.assertTrue(self.check("chat_window: true\n"))

    def test_false(self):
        self.assertFalse(self.check("chat_window: false\n"))

    def test_missing_key_means_console(self):
        self.assertFalse(self.check("overlay: true\n"))

    def test_malformed_yaml(self):
        self.assertFalse(self.check("chat_window: [unclosed\n  : :\n"))

    def test_not_a_mapping(self):
        self.assertFalse(self.check("- a\n- b\n"))

    def test_string_true_is_not_true(self):
        self.assertFalse(self.check('chat_window: "yes please"\n'))

    def test_missing_file(self):
        self.assertFalse(self.check(None))

    def test_overlay_off_blocks_windowless(self):
        # no status box = no chat window = no way to quit, so stay in the console
        self.assertFalse(self.check("chat_window: true\noverlay: false\n"))

    def test_real_config_exit_code_matches(self):
        r = subprocess.run([sys.executable, str(HERE / "windowless.py"), "check"], capture_output=True)
        expect = 0 if (HERE / "config.yaml").exists() and windowless.chat_window_enabled(str(HERE / "config.yaml")) else 1
        self.assertEqual(r.returncode, expect)


class ScriptTests(unittest.TestCase):
    def test_start_bat_wiring(self):
        bat = (HERE / "start.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("windowless.py check", bat)
        self.assertIn('start "" ".venv\\Scripts\\pythonw.exe" voice_claude.py', bat)
        # the windowless branch must leave before the console fallback + pause
        self.assertLess(bat.index("pythonw.exe"), bat.index("voice_claude.py\r\npause" if "voice_claude.py\r\npause" in bat
                                                           else "voice_claude.py\npause"))
        self.assertIn("pause", bat)

    def test_start_console_is_old_behaviour(self):
        old = (HERE / "start_console.bat").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("pythonw", old)
        self.assertIn('".venv\\Scripts\\python.exe" voice_claude.py', old)
        self.assertRegex(old, r"voice_claude\.py\s+pause")
        self.assertIn("start_console.bat", old)

    def test_restart_pattern_matches_pythonw_and_python_not_others(self):
        ps = (HERE / "restart_app.ps1").read_text(encoding="utf-8")
        self.assertIn("-match '^python'", ps)
        pat = re.compile("^python", re.I)                          # PowerShell -match is case-insensitive
        for name in ("python.exe", "pythonw.exe", "Pythonw.EXE"):
            self.assertTrue(pat.search(name), name)
        for name in ("cmd.exe", "powershell.exe", "wpython.exe"):
            self.assertFalse(pat.search(name), name)
        self.assertRegex(ps, r"voice_claude\\\.py")
        self.assertIn("start.bat", ps)


class VoiceAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import voice_claude as vc
        cls.vc = vc

    def test_show_and_log_safe_without_stdout(self):
        vc = self.vc
        old_out, old_w, old_dir = sys.stdout, vc.WINDOWLESS, vc.LOG_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                vc.LOG_DIR = pathlib.Path(d)
                sys.stdout = None
                for w in (True, False):                   # windowless: skipped; console flag with a dead stdout: swallowed
                    vc.WINDOWLESS = w
                    for who in ("You", "Claude", "system"):
                        vc.log(who, "hello")
                logs = list(pathlib.Path(d).glob("*.txt"))
                self.assertEqual(len(logs), 1)
                self.assertGreaterEqual(logs[0].read_text(encoding="utf-8").count("hello"), 6)   # file log still works
        finally:
            sys.stdout, vc.WINDOWLESS, vc.LOG_DIR = old_out, old_w, old_dir

    def test_show_still_mirrors_to_chat_window_when_windowless(self):
        vc = self.vc
        fake_cw = mock.Mock()
        with mock.patch.object(vc, "chat_window", fake_cw), mock.patch.object(vc, "WINDOWLESS", True):
            vc.show("You", "hi")
        fake_cw.feed.assert_called_once()

    def test_quit_app_closes_overlay_and_backstop(self):
        vc = self.vc
        root = mock.Mock()
        app = SimpleNamespace(mouth=mock.Mock(), brain=mock.Mock(), overlay=SimpleNamespace(root=root))
        with mock.patch.object(vc, "log"), mock.patch.object(vc.threading, "Timer") as timer:
            vc.App.quit_app(app, "test")
        app.mouth.stop.assert_called_once()
        app.brain.stop.assert_called_once()
        root.after.assert_called_once_with(0, root.destroy)
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()

    def test_voice_quit_needs_confirmation(self):
        vc = self.vc
        app = SimpleNamespace(mouth=mock.Mock(), quit_app=mock.Mock())
        app.quit_gate = windowless.QuitGate()
        check = lambda t: vc.App._quit_gate_check(app, t)
        self.assertFalse(check("what time is it"))            # not ours
        self.assertTrue(check("Shut down the assistant."))    # asks
        app.mouth.say.assert_called_once()
        app.quit_app.assert_not_called()                      # nothing happens until confirmed
        self.assertTrue(check("Confirmed."))
        app.quit_app.assert_called_once()

    def test_chat_window_confirm_quit_yes_no(self):
        import chat_window
        app = SimpleNamespace(quit_app=mock.Mock())
        cw = chat_window.ChatWindow.__new__(chat_window.ChatWindow)
        cw.app = app
        self.assertFalse(cw.confirm_quit(ask=lambda: False))
        app.quit_app.assert_not_called()
        self.assertTrue(cw.confirm_quit(ask=lambda: True))
        app.quit_app.assert_called_once_with("Quit button")
        cw.app = SimpleNamespace()                            # app without quit_app: no crash
        self.assertFalse(cw.confirm_quit(ask=lambda: True))


class QuitGateTests(unittest.TestCase):
    def test_phrases_that_ask(self):
        for p in ("quit", "Quit AIDA", "ok quit the assistant", "shut down the assistant", "Shut down AIDA.",
                  "turn off the voice app", "power down this program"):
            self.assertEqual(windowless.QuitGate().handle(p, now=0), "ask", p)

    def test_phrases_that_must_not_ask(self):
        # "stop" is stop-talking, "close VS Code" is an instant command, "shut down" alone is Windows shutdown
        for p in ("stop", "close VS Code", "shut down", "shut down the computer", "quit smoking", "exit", ""):
            self.assertIsNone(windowless.QuitGate().handle(p, now=0), p)

    def test_confirm_in_time_quits(self):
        g = windowless.QuitGate()
        self.assertEqual(g.handle("quit", now=100), "ask")
        self.assertEqual(g.handle("Yes, confirmed.", now=130), "quit")

    def test_confirm_too_late_does_nothing(self):
        g = windowless.QuitGate()
        g.handle("quit", now=100)
        self.assertIsNone(g.handle("confirmed", now=161))

    def test_confirm_without_ask_does_nothing(self):
        self.assertIsNone(windowless.QuitGate().handle("confirmed", now=5))

    def test_other_text_cancels_pending_quit(self):
        g = windowless.QuitGate()
        g.handle("quit", now=0)
        self.assertIsNone(g.handle("what's the weather", now=5))
        self.assertIsNone(g.handle("confirmed", now=6))        # pending was cancelled

    def test_confirm_is_single_use(self):
        g = windowless.QuitGate()
        g.handle("quit", now=0)
        self.assertEqual(g.handle("confirmed", now=1), "quit")
        self.assertIsNone(g.handle("confirmed", now=2))


if __name__ == "__main__":
    unittest.main()
