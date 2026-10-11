"""Tests for phone_link.py. Network is fully mocked. Run:  .venv\\Scripts\\python.exe -m unittest test_phone_link -v"""
import json
import tempfile
import unittest
import pathlib
import datetime

import phone_link as pl

ME, STRANGER = 111222333, 999


def upd(uid, text="hello", sender=ME, chat_id=None, chat_type="private"):
    return {"update_id": uid, "message": {"message_id": uid, "from": {"id": sender}, "text": text,
                                          "chat": {"id": sender if chat_id is None else chat_id, "type": chat_type}}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.sent, self.asked, self.logs = [], [], []
        self.link = self.make()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, reply="Sure thing."):
        def ask(text):
            self.asked.append(text)
            return reply

        def api(method, params, timeout):
            if method == "sendMessage":
                self.sent.append(params["text"])
            return {"ok": True, "result": []}
        return pl.PhoneLink(ask, lambda w, t: self.logs.append((w, t)), api=api, token="SECRET:TOKEN",
                            allowed_id=ME, state_dir=self.dir, helpers_dir=self.dir / "helpers", threaded=False)


class TestAllowlist(Base):
    def test_allowed_user_is_passed_on_marked_from_phone(self):
        # Allowlisted ID in a private chat -> assistant gets the text, prefixed as FROM PHONE; reply is sent back.
        self.link.handle_update(upd(1, "what's the weather like"))
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(self.asked[0].startswith("[FROM PHONE"))
        self.assertIn("what's the weather like", self.asked[0])
        self.assertEqual(self.sent, ["Sure thing."])

    def test_stranger_rejected_silently(self):
        # Unknown sender -> not forwarded, no reply at all (don't confirm the bot exists), text never logged.
        self.link.handle_update(upd(1, "hi there", sender=STRANGER))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.sent, [])
        self.assertFalse(any("hi there" in t for _, t in self.logs))

    def test_allowed_id_in_group_chat_rejected(self):
        # Right user but a group chat -> rejected (only the private chat counts).
        self.link.handle_update(upd(1, "hi", chat_id=-5, chat_type="group"))
        self.assertEqual(self.asked, [])

    def test_chat_id_mismatch_rejected(self):
        # Right sender ID but chat ID differs -> rejected.
        self.link.handle_update(upd(1, "hi", chat_id=12345))
        self.assertEqual(self.asked, [])

    def test_sender_id_as_string_rejected(self):
        # Type confusion: "111222333" (str) must not equal the int allowlist.
        u = upd(1)
        u["message"]["from"]["id"] = str(ME)
        self.link.handle_update(u)
        self.assertEqual(self.asked, [])


class TestGuardRails(Base):
    def test_guard_words_refused(self):
        # Each guard-rail verb is refused, never reaches the assistant, and the reply points at the desk.
        for i, txt in enumerate(["delete my old projects", "send an email to Liz", "post this on Slack",
                                 "buy me a new mouse", "shut down the PC", "shutdown now",
                                 "post the weekly report", "confirmed", "please uninstall chrome"], 1):
            with self.subTest(txt=txt):
                self.link.handle_update(upd(i, txt))
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.sent), 9)
        self.assertTrue(all("desk" in s and "confirmed" in s for s in self.sent))

    def test_innocent_words_not_blocked(self):
        # Word boundaries: "postcode" / "sender" / "formatting" must not trip the guard.
        for i, txt in enumerate(["what is my postcode", "who is the sender of that", "nice formatting"], 1):
            self.link.handle_update(upd(i, txt))
        self.assertEqual(len(self.asked), 3)

    def test_guard_case_insensitive(self):
        # SHOUTED guard word still refused.
        self.link.handle_update(upd(1, "DELETE everything"))
        self.assertEqual(self.asked, [])


class TestMarkdown(unittest.TestCase):
    def test_strip_markdown(self):
        # Bold, headings, bullets, links, inline code and fences are all flattened.
        src = "# Title\n**Bold** and `code`\n- item one\n1. item two\n[link](http://x.y)\n```\nbig\ncode\n```"
        out = pl.strip_markdown(src)
        for bad in ("**", "`", "# ", "](", "- item", "big\ncode"):
            self.assertNotIn(bad, out)
        self.assertIn("Bold and code", out)
        self.assertIn("link", out)

    def test_shorten_prefers_sentence_and_caps(self):
        # Long reply is cut near the limit at a sentence boundary and flagged.
        long = ("This is a sentence. " * 100).strip()
        out = pl.shorten(long, 200)
        self.assertLess(len(out), 260)
        self.assertIn("full reply is on the PC", out)

    def test_empty_reply_becomes_done(self):
        # Nothing to say -> "Done." rather than an empty message (Telegram rejects empty text).
        self.assertEqual(pl.format_reply("   "), "Done.")
        self.assertEqual(pl.format_reply(None), "Done.")

    def test_reply_never_exceeds_telegram_limit(self):
        self.assertLessEqual(len(pl.format_reply("x" * 20000)), pl.TG_LIMIT)


class TestMalformed(Base):
    def test_malformed_updates_ignored(self):
        # None of these may raise, ask the assistant or send anything.
        bad = [None, 5, "str", [], {}, {"update_id": "1"}, {"update_id": True},
               {"update_id": 1}, {"update_id": 2, "message": "x"},
               {"update_id": 3, "message": {"text": "hi"}},
               {"update_id": 4, "message": {"from": "me", "chat": {}, "text": "hi"}},
               {"update_id": 5, "message": {"from": {"id": ME}, "chat": None, "text": "hi"}},
               {"update_id": 6, "edited_message": {"text": "hi"}}]
        for b in bad:
            self.link.handle_update(b)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.sent, [])

    def test_non_text_message_gets_polite_reply(self):
        # Allowed user sends a photo/sticker (no text) -> short "text only", assistant not called.
        u = upd(1)
        del u["message"]["text"]
        self.link.handle_update(u)
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.sent), 1)

    def test_text_wrong_type(self):
        u = upd(1)
        u["message"]["text"] = 12345
        self.link.handle_update(u)
        self.assertEqual(self.asked, [])

    def test_poll_not_ok_raises_for_backoff(self):
        # Telegram says ok:false -> poll_once raises so run() backs off.
        self.link.api = lambda m, p, t: {"ok": False}
        with self.assertRaises(RuntimeError):
            self.link.poll_once(1)


class TestDuplicates(Base):
    def test_duplicate_update_id_processed_once(self):
        # Same update delivered twice -> one assistant turn only.
        self.link.handle_update(upd(7, "hello"))
        self.link.handle_update(upd(7, "hello"))
        self.assertEqual(len(self.asked), 1)

    def test_older_update_id_ignored(self):
        self.link.handle_update(upd(10, "a"))
        self.link.handle_update(upd(9, "b"))
        self.assertEqual(len(self.asked), 1)

    def test_offset_survives_restart(self):
        # Offset persisted to disk -> a fresh instance won't replay old messages.
        self.link.handle_update(upd(42, "a"))
        again = self.make()
        again.handle_update(upd(42, "a"))
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(again.last_update_id, 42)

    def test_poll_requests_next_offset(self):
        # getUpdates must ask for last_id + 1 so Telegram discards handled updates.
        calls = []
        self.link.last_update_id = 5
        self.link.api = lambda m, p, t: (calls.append((m, p)) or {"ok": True, "result": []})
        self.link.poll_once(1)
        self.assertEqual(calls[0][1]["offset"], 6)


class TestSecrets(unittest.TestCase):
    def test_token_missing(self):
        # No env, no file -> (None, None); start() refuses to run and logs without leaking anything.
        with tempfile.TemporaryDirectory() as d:
            tok, uid = pl.load_secrets(env={}, secrets_file=pathlib.Path(d) / "nope.env")
        self.assertIsNone(tok)
        self.assertIsNone(uid)

    def test_start_without_secrets_returns_none(self):
        old_env = dict(pl.os.environ)
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID"):
            pl.os.environ.pop(k, None)
        old_file = pl.SECRETS_FILE
        pl.SECRETS_FILE = pathlib.Path("Z:/definitely/missing.env")
        logs = []
        try:
            # load_secrets default arg was bound at def time, so patch via env only (file absent in test setup)
            import unittest.mock as m
            with m.patch.object(pl, "load_secrets", return_value=(None, None)):
                self.assertIsNone(pl.start(lambda t: "", lambda w, t: logs.append(t)))
        finally:
            pl.SECRETS_FILE = old_file
            pl.os.environ.update(old_env)
        self.assertTrue(any("missing" in l for l in logs))

    def test_secrets_from_env(self):
        tok, uid = pl.load_secrets(env={"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_ALLOWED_USER_ID": "42"},
                                   secrets_file="Z:/missing.env")
        self.assertEqual((tok, uid), ("abc", 42))

    def test_secrets_from_file_and_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "s.env"
            f.write_text("# c\nTELEGRAM_BOT_TOKEN=filetoken\nTELEGRAM_ALLOWED_USER_ID = 7\n", encoding="utf-8")
            self.assertEqual(pl.load_secrets(env={}, secrets_file=f), ("filetoken", 7))
            self.assertEqual(pl.load_secrets(env={"TELEGRAM_BOT_TOKEN": "envtoken"}, secrets_file=f),
                             ("envtoken", 7))

    def test_non_numeric_user_id_rejected(self):
        _, uid = pl.load_secrets(env={"TELEGRAM_BOT_TOKEN": "a", "TELEGRAM_ALLOWED_USER_ID": "abc"},
                                 secrets_file="Z:/missing.env")
        self.assertIsNone(uid)



class TestTokenScrub(Base):
    def test_token_never_in_logs_on_errors(self):
        # API failure whose message contains the token URL -> scrubbed from the log (send and run-loop paths).
        def boom(m, p, t):
            raise OSError("failed https://api.telegram.org/botSECRET:TOKEN/getUpdates")
        self.link.api = boom
        self.link.send("x")
        self.link._stop.set()                       # run() would exit after one pass; force a single failing pass
        self.link._stop.clear()
        try:
            self.link.poll_once(1)
        except OSError as e:
            self.link.log("phone", "poll problem: " + self.link._scrub(e))
        self.assertTrue(self.logs)
        self.assertFalse(any("SECRET:TOKEN" in t for _, t in self.logs))


class TestStatusAndReminders(Base):
    def test_status_without_ledger(self):
        # No state/helpers directory -> graceful message, no exception.
        self.link.handle_update(upd(1, "/status"))
        self.assertIn("No helper ledger", self.sent[0])
        self.assertEqual(self.asked, [])

    def test_status_with_ledger_and_corrupt_file(self):
        # Running jobs are listed, done ones skipped, corrupt JSON ignored.
        h = self.dir / "helpers"
        h.mkdir()
        (h / "a.json").write_text(json.dumps({"id": "a", "title": "Build thing", "status": "running",
                                              "latest_note": "halfway"}))
        (h / "b.json").write_text(json.dumps({"id": "b", "title": "Old", "status": "done"}))
        (h / "c.json").write_text("{not json")
        self.link.handle_update(upd(1, "/status"))
        self.assertIn("Build thing", self.sent[0])
        self.assertNotIn("Old", self.sent[0])

    def test_status_with_none_running(self):
        h = self.dir / "helpers"
        h.mkdir()
        (h / "b.json").write_text(json.dumps({"status": "done", "title": "x"}))
        self.assertIn("No helpers running", self.link.status_text())

    def test_status_command_from_stranger_ignored(self):
        self.link.handle_update(upd(1, "/status", sender=STRANGER))
        self.assertEqual(self.sent, [])

    def test_parse_remind_relative_and_clock(self):
        now = datetime.datetime(2026, 10, 10, 17, 0)
        self.assertEqual(pl.parse_remind("in 30m ring mum", now), (now + datetime.timedelta(minutes=30), "ring mum"))
        self.assertEqual(pl.parse_remind("in 2 hours tea", now)[0], now + datetime.timedelta(hours=2))
        self.assertEqual(pl.parse_remind("18:30 tea", now)[0], datetime.datetime(2026, 10, 10, 18, 30))
        self.assertEqual(pl.parse_remind("09:00 gym", now)[0], datetime.datetime(2026, 10, 11, 9, 0))  # next day

    def test_parse_remind_invalid(self):
        for bad in ("", "25:00 x", "in x m y", "tomorrow", "18:30"):
            self.assertIsNone(pl.parse_remind(bad), bad)

    def test_reminder_fires_once_and_persists(self):
        t = [datetime.datetime(2026, 10, 10, 17, 0)]
        self.link.clock = lambda: t[0]
        self.link.handle_update(upd(1, "/remind in 10m take bins out"))
        self.assertIn("Reminder set", self.sent[-1])
        self.link.fire_reminders()
        self.assertEqual(len(self.sent), 1)                 # not due yet
        t[0] += datetime.timedelta(minutes=11)
        self.link.fire_reminders()
        self.link.fire_reminders()
        self.assertEqual(self.sent[-1], "Reminder: take bins out")
        self.assertEqual(len(self.sent), 2)                 # fired exactly once
        self.assertEqual(json.loads((self.dir / "phone_reminders.json").read_text()), [])

    def test_remind_text_with_guard_word_is_allowed(self):
        # /remind only stores text, nothing is executed, so guard words in it are fine.
        self.link.handle_update(upd(1, "/remind in 5m send the invoice"))
        self.assertIn("Reminder set", self.sent[0])

    def test_corrupt_reminder_dropped(self):
        self.link.reminders = [{"due": "garbage", "text": "x"}, {"text": "no due"}]
        self.link.fire_reminders()
        self.assertEqual(self.link.reminders, [])

    def test_unknown_command(self):
        self.link.handle_update(upd(1, "/wat"))
        self.assertIn("Unknown", self.sent[0])
        self.assertEqual(self.asked, [])


class TestBusyAndErrors(Base):
    def test_busy_second_message(self):
        # While a turn is running a second message gets "still on the last one", not queued twice.
        self.link._busy.acquire()
        self.link.handle_update(upd(1, "hello"))
        self.assertIn("Still on the last one", self.sent[0])
        self.assertEqual(self.asked, [])

    def test_ask_exception_reported_and_lock_released(self):
        def bad(text):
            raise RuntimeError("kaboom SECRET:TOKEN")
        self.link.ask = bad
        self.link.handle_update(upd(1, "hello"))
        self.assertIn("failed", self.sent[0])
        self.assertFalse(any("SECRET:TOKEN" in t for _, t in self.logs))
        self.link.ask = lambda t: "ok"
        self.link.handle_update(upd(2, "again"))
        self.assertEqual(self.sent[-1], "ok")

    def test_markdown_reply_is_flattened_before_sending(self):
        self.link.ask = lambda t: "**Done.** Here:\n- one\n- two"
        self.link.handle_update(upd(1, "hello"))
        self.assertNotIn("**", self.sent[0])
        self.assertNotIn("- one", self.sent[0])


if __name__ == "__main__":
    unittest.main()
