"""Voice / typed commands for the global Mute override. Pure, no Tk, no audio.

'stop' / 'shut up' / 'quiet' are NOT handled here: they only cut the current speech (App._local_command).
Mute is a separate, sticky state that is never saved (the assistant always starts un-muted). It silences the
speaker only (audio_mute.py): speech carries on in real time and unmuting resumes at the current position.
"""
import re

MUTE_TIP = "Silences the speaker. I keep talking, so unmuting picks up where I've got to."
MUTE_LABEL = "Mute"


def parse_command(text):
    """'mute' / 'mute yourself' -> True; 'unmute' / 'unmute yourself' -> False; anything else -> None."""
    t = re.sub(r"[^\w\s]", "", (text or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r" please$", "", t)
    m = re.fullmatch(r"(?:ok |okay |hey |please )?(un)?mute(?: yourself| your voice| the voice| aida)?", t)
    if m:
        return not m.group(1)
    return None
