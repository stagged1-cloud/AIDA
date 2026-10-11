"""
phone_link: message the assistant from the user's phone through a private Telegram bot.

Outbound HTTPS long polling only (getUpdates), so nothing listens on the PC and no ports are opened.
Disabled by default: set phone_link_enabled: true in config.yaml once setup (phone_link_setup.md) is done.

Secrets (never hardcoded, never logged), looked up in this order:
  1. environment variables  TELEGRAM_BOT_TOKEN  and  TELEGRAM_ALLOWED_USER_ID
  2. state/phone_secrets.env (gitignored), lines like  TELEGRAM_BOT_TOKEN=...

Only the allowlisted numeric user ID (in a private chat) is ever answered; everyone else is silently dropped.
Guard-rail actions (delete, send, post, buy, shutdown) are refused here: they need the user at
the desk saying "confirmed" by voice. "confirmed" is never forwarded from the phone.

Commands:  /status   active helpers (reads state/helpers/*.json if it exists)
           /remind in 30m <text>   or   /remind 18:30 <text>      /reminders      /help
"""
import os
import re
import json
import pathlib
import datetime
import threading
import urllib.request
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
SECRETS_FILE = STATE_DIR / "phone_secrets.env"
HELPERS_DIR = STATE_DIR / "helpers"

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 50           # seconds of server-side long poll
MAX_REPLY = 700             # keep phone replies short
TG_LIMIT = 4000             # Telegram caps a message at 4096 chars

PHONE_PREFIX = ("[FROM PHONE: The user is away from the desk and reading this as a text message. Reply in plain text, "
                "no markdown, two or three short sentences. You cannot ask them to say 'confirmed' here: delete, send, "
                "post, buy, shutdown and anything touching work systems must wait until they are at the desk.]\n")

GUARD_RE = re.compile(
    r"\b(delete|deleting|remove|erase|wipe|uninstall|format|"
    r"send|email|e-mail|post|tweet|reply\s+to|"
    r"buy|purchase|pay|order|"
    r"shut\s?down|power\s?off|reboot|restart|"
    r"confirmed?)\b", re.I)

GUARD_REPLY = ("That one needs you at the desk. Delete, send, post, buy, shutdown and work-system "
               "stuff can only be approved by saying 'confirmed' by voice.")


# ---------------------------------------------------------------- pure helpers
def strip_markdown(text):
    """Plain text for a phone: no code fences, bold, headings, bullets or link syntax."""
    t = text or ""
    t = re.sub(r"```.*?```", " (details are on the PC) ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\(([^)]*)\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|~~)", "", t)
    t = re.sub(r"^\s*(?:[-*•]|\d+\.)\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\|?[-:| ]{3,}\|?\s*$", "", t, flags=re.M)      # table rules
    t = t.replace("|", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def shorten(text, limit=MAX_REPLY):
    """Cut to limit, preferring a sentence boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if m > limit * 0.5:
        cut = cut[:m + 1]
    return cut.rstrip() + " ... (full reply is on the PC)"


def format_reply(text):
    t = shorten(strip_markdown(text))
    return t[:TG_LIMIT] if t else "Done."


def is_guarded(text):
    return bool(GUARD_RE.search(text or ""))


def parse_remind(args, now=None):
    """'in 30m text' / 'in 2h text' / '18:30 text' -> (datetime, text) or None."""
    now = now or datetime.datetime.now()
    m = re.match(r"^in\s+(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)\s+(.+)$", args.strip(), re.I | re.S)
    if m:
        n = int(m.group(1))
        delta = datetime.timedelta(hours=n) if m.group(2).lower().startswith("h") else datetime.timedelta(minutes=n)
        return now + delta, m.group(3).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})\s+(.+)$", args.strip(), re.S)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return None
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if due <= now:
            due += datetime.timedelta(days=1)
        return due, m.group(3).strip()
    return None


def load_secrets(env=None, secrets_file=SECRETS_FILE):
    """(token, allowed_id) from env, then the gitignored file. Missing -> None."""
    env = os.environ if env is None else env
    vals = {k: env.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID")}
    try:
        for line in pathlib.Path(secrets_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in vals and not vals[k]:
                    vals[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    token = (vals["TELEGRAM_BOT_TOKEN"] or "").strip() or None
    try:
        uid = int(str(vals["TELEGRAM_ALLOWED_USER_ID"]).strip())
    except (TypeError, ValueError):
        uid = None
    return token, uid


# ---------------------------------------------------------------- the link
class PhoneLink:
    def __init__(self, ask, log=lambda who, text: None, api=None, token=None, allowed_id=None,
                 state_dir=STATE_DIR, helpers_dir=HELPERS_DIR, threaded=True, clock=datetime.datetime.now):
        """ask(text) -> reply string (or None); runs one assistant turn. api(method, params, timeout) -> dict."""
        self.ask, self.log = ask, log
        self.token, self.allowed_id = token, allowed_id
        self.api = api or self._http_api
        self.state_dir = pathlib.Path(state_dir)
        self.helpers_dir = pathlib.Path(helpers_dir)
        self.threaded = threaded
        self.clock = clock
        self.last_update_id = self._load_json("phone_offset.json", {}).get("last", 0)
        self.reminders = self._load_json("phone_reminders.json", [])
        self._busy = threading.Lock()
        self._stop = threading.Event()

    # -- state files
    def _load_json(self, name, default):
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save_json(self, name, data):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / name).write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            self.log("phone", f"couldn't save {name}: {e}")

    def _scrub(self, s):
        s = str(s)
        return s.replace(self.token, "<token>") if self.token else s

    # -- network (the only place the token is used)
    def _http_api(self, method, params, timeout):
        url = f"{API_BASE}/bot{self.token}/{method}"
        req = urllib.request.Request(url, data=json.dumps(params).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def send(self, text):
        try:
            self.api("sendMessage", {"chat_id": self.allowed_id, "text": text[:TG_LIMIT]}, 20)
        except Exception as e:
            self.log("phone", "send failed: " + self._scrub(e))

    # -- incoming
    def handle_update(self, upd):
        """Process one raw update. Never raises on malformed input."""
        if not isinstance(upd, dict):
            return
        uid = upd.get("update_id")
        if not isinstance(uid, int) or isinstance(uid, bool):
            return
        if uid <= self.last_update_id:                      # duplicate / replay
            return
        self.last_update_id = uid
        self._save_json("phone_offset.json", {"last": uid})
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        sender, chat, text = msg.get("from"), msg.get("chat"), msg.get("text")
        sid = sender.get("id") if isinstance(sender, dict) else None
        if (not isinstance(sid, int) or sid != self.allowed_id or not isinstance(chat, dict)
                or chat.get("type") != "private" or chat.get("id") != self.allowed_id):
            self.log("phone", "ignored a message from a non-allowlisted sender")   # no ID/text logged, no reply
            return
        if not isinstance(text, str) or not text.strip():
            self.send("Text only, I'm afraid.")
            return
        self.dispatch(text.strip())

    def dispatch(self, text):
        if text.startswith("/"):
            cmd, _, args = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd in ("/start", "/help"):
                return self.send("Send a message and I'll pass it on. /status, /remind in 30m <text>, "
                                 "/remind 18:30 <text>, /reminders.")
            if cmd == "/status":
                return self.send(self.status_text())
            if cmd == "/remind":
                return self.send(self.add_reminder(args))
            if cmd == "/reminders":
                return self.send(self.list_reminders())
            return self.send("Unknown command. Try /help.")
        if is_guarded(text):
            self.log("phone", "refused a guard-rail request from the phone")
            return self.send(GUARD_REPLY)
        if self.threaded:
            threading.Thread(target=self._run_turn, args=(text,), daemon=True).start()
        else:
            self._run_turn(text)

    def _run_turn(self, text):
        if not self._busy.acquire(blocking=False):
            return self.send("Still on the last one. Give me a minute.")
        try:
            self.log("phone", "message from phone: " + text)
            reply = self.ask(PHONE_PREFIX + text)
            self.send(format_reply(reply or ""))
        except Exception as e:
            self.log("phone", "turn failed: " + self._scrub(e))
            self.send("That failed at the PC. Details are in the log.")
        finally:
            self._busy.release()

    # -- /status
    def status_text(self):
        try:
            files = sorted(self.helpers_dir.glob("*.json"))
        except OSError:
            files = []
        if not files:
            return "No helper ledger yet, so nothing to report."
        live, lines = 0, []
        for f in files:
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(job, dict):
                    continue
            except (OSError, ValueError):
                continue
            st = str(job.get("status", "?"))
            if st in ("running", "stale"):
                live += 1
                lines.append(f"{st}: {str(job.get('title', '?'))[:60]}"
                             + (f" - {str(job.get('latest_note'))[:80]}" if job.get("latest_note") else ""))
        if not live:
            return "No helpers running."
        return f"{live} helper(s) active.\n" + "\n".join(lines[:8])

    # -- reminders
    def add_reminder(self, args):
        parsed = parse_remind(args, self.clock())
        if not parsed:
            return "Usage: /remind in 30m <text>  or  /remind 18:30 <text>"
        due, text = parsed
        self.reminders.append({"due": due.isoformat(timespec="seconds"), "text": text[:300]})
        self._save_json("phone_reminders.json", self.reminders)
        return f"Reminder set for {due:%a %H:%M}."

    def list_reminders(self):
        if not self.reminders:
            return "No reminders."
        return "\n".join(f"{r['due'][5:16].replace('T', ' ')} {r['text'][:60]}" for r in self.reminders[:10])

    def fire_reminders(self):
        now, keep, fired = self.clock(), [], False
        for r in self.reminders:
            try:
                due = datetime.datetime.fromisoformat(r["due"])
            except (KeyError, ValueError, TypeError):
                continue                                    # drop corrupt entries
            if due <= now:
                self.send("Reminder: " + str(r.get("text", "")))
                fired = True
            else:
                keep.append(r)
        if fired or len(keep) != len(self.reminders):
            self.reminders = keep
            self._save_json("phone_reminders.json", keep)

    # -- loop
    def poll_once(self, timeout=POLL_TIMEOUT):
        resp = self.api("getUpdates", {"offset": self.last_update_id + 1, "timeout": timeout,
                                       "allowed_updates": ["message"]}, timeout + 10)
        if not isinstance(resp, dict) or not resp.get("ok"):
            raise RuntimeError("getUpdates not ok")
        for upd in resp.get("result") or []:
            try:
                self.handle_update(upd)
            except Exception as e:
                self.log("phone", "update error: " + self._scrub(e))

    def run(self):
        backoff = 2
        self.log("phone", "phone link running (outbound polling only)")
        while not self._stop.is_set():
            try:
                self.fire_reminders()
                self.poll_once()
                backoff = 2
            except Exception as e:
                self.log("phone", "poll problem: " + self._scrub(e))
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def stop(self):
        self._stop.set()


def start(ask, log=lambda who, text: None):
    """Start the link on a daemon thread. Returns the PhoneLink, or None if secrets are missing."""
    token, uid = load_secrets()
    if not token or uid is None:
        log("phone", "phone link enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID are missing; not started")
        return None
    link = PhoneLink(ask, log, token=token, allowed_id=uid)
    threading.Thread(target=link.run, daemon=True, name="phone_link").start()
    return link
