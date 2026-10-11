"""Decide whether a turn is simple enough for the fast model (haiku). Pure: no I/O, no globals.

is_simple(text) -> True only for short, single-step orders and chit-chat (open/close/play/stop/record/
screenshot/status, greetings, thanks). Anything else, or anything unsure, is complex and stays on the
main model. Also builds/parses the stream-json set_model control request used to switch mid-session.
"""
import json
import re

FAST_MODEL_DEFAULT = "claude-haiku-5-5"
MAX_WORDS = 14                      # longer than this = complex

# Any of these anywhere = complex (word-start match, so "fixed", "testing", "builds" all count).
COMPLEX_WORDS = (
    "think hard", "code", "coding", "build", "test", "fix", "bug", "debug", "research", "ticket",
    "email", "e-mail", "mail", "work", "zendesk", "log", "helper", "agent", "worker",
    "commit", "push", "pull", "install", "deploy", "refactor", "implement", "review", "explain",
    "why", "analyse", "analyze", "summar", "write", "draft", "reply", "calendar", "drive", "jira",
    "confluence", "teams", "slack", "script", "config", "edit", "change", "update", "delete",
    "remove", "file", "folder", "project", "plan", "design", "error", "crash", "check", "read",
    "remember", "memory", "search", "find", "look up", "compare", "github", "git", "restart",
    "reboot", "confirmed", "screen watch", "customer", "server", "gateway", "site 24",
)

# Multi-step joiners: "and then", "then", "after that", "once that's done", "also".
MULTI_STEP = re.compile(r"\b(and then|then|after that|afterwards|once (?:that|it)|also|next|followed by)\b")

# The turn must start with one of these (after fillers) to be simple.
SIMPLE_STARTS = (
    "open", "close", "play", "pause", "resume", "stop", "record", "start recording", "screenshot",
    "take a screenshot", "take a screen print", "screen print", "status", "what's the time", "what time",
    "what's the date", "what day", "how's it going", "how are you", "where we at", "where are we at",
    "what you doing", "what are you doing", "you there", "are you there", "are you on line",
    "are you online", "hello", "hi", "hey", "morning", "good morning", "evening", "good night",
    "night", "thanks", "thank you", "cheers", "nice one", "good job", "well done", "ok", "okay",
    "cool", "fair enough", "shut up", "volume", "mute", "unmute", "go to", "show me", "minimise",
    "minimize", "maximise", "maximize",
)

FILLERS = re.compile(r"^(?:(?:um+|uh+|er+|erm|so|right|ok|okay|yeah|hey aida|aida|please|can you|could you|"
                     r"will you|just|now)[,.!? ]+)*")


def _norm(text):
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"\[[^\]]*\]", " ", t)                   # drop any [header] the app adds
    t = re.sub(r"\bvs code\b", "vscode", t)             # the app name is not a coding job
    return re.sub(r"\s+", " ", t).strip()


def is_simple(text, max_words=MAX_WORDS):
    """True = safe for the fast model. When unsure, False."""
    t = _norm(text)
    if not t:
        return False
    words = re.findall(r"[a-z0-9']+", t)
    if not words or len(words) > max_words:
        return False
    if t.count("?") > 1 or t.count(",") > 2:          # stacked questions / long run-on
        return False
    for w in COMPLEX_WORDS:
        if re.search(r"\b" + re.escape(w), t):
            return False
    if MULTI_STEP.search(t):
        return False
    body = FILLERS.sub("", t + " ").strip(" ,.!?")
    if not body:                                      # nothing but filler ("ok.", "yeah") = chit-chat
        return True
    return any(body == s or body.startswith(s + " ") or body.startswith(s + ",") for s in SIMPLE_STARTS)


def target_model(text, main_model, fast_model=FAST_MODEL_DEFAULT, enabled=True, mode="normal"):
    """Which model should serve this turn of the always-on session. Deep/connector turns never use fast."""
    if not enabled or mode != "normal" or not fast_model:
        return main_model
    return fast_model if is_simple(text) else main_model


def set_model_request(model, request_id):
    """One stream-json line asking the running session to switch model."""
    return json.dumps({"type": "control_request", "request_id": request_id,
                       "request": {"subtype": "set_model", "model": model}}) + "\n"


def run_switch(write, events, model, request_id, timeout_s=5.0, on_other=lambda ev: None, clock=None):
    """Send set_model and wait for its answer. write(line) sends; events is a queue.Queue of parsed events
    (None = stream ended). Other events go to on_other. Returns True only on a success reply in time.
    Any exception, error reply, stream end or timeout = False (caller stays on the current model)."""
    import queue as _q
    import time as _t
    clock = clock or _t.time
    try:
        write(set_model_request(model, request_id))
        deadline = clock() + float(timeout_s)
        while clock() < deadline:
            try:
                ev = events.get(timeout=0.3)
            except _q.Empty:
                continue
            if ev is None:
                return False
            r = control_ok(ev, request_id)
            if r is None:
                on_other(ev)
                continue
            return r
    except Exception:
        return False
    return False


def control_ok(ev, request_id):
    """None = not the reply we're waiting for; True/False = success/failure for request_id."""
    if not isinstance(ev, dict) or ev.get("type") != "control_response":
        return None
    r = ev.get("response") or {}
    if r.get("request_id") != request_id:
        return None
    return r.get("subtype") == "success"
