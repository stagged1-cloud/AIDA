"""Size caps for the always-loaded memory core (small always-loaded core, 11 Oct 2026).

assistant/CLAUDE.md loads only a small core every turn. The full speech profile lives in
memory/topics/full/how_you_talk.md; the loaded memory/topics/how_you_talk.md is a distilled copy
that must stay under PROFILE_CAP characters. The learner is asked to keep it under the cap; this
module enforces it in code so the file can't bloat again if the model ignores the instruction.
"""
import re

PROFILE_CAP = 6000      # chars (~1,500 tokens) for the loaded, distilled speech profile
RULES_CAP = 4000        # chars (~1,000 tokens) for topics/core_rules.md


def cap_text(text, cap):
    """Return text trimmed to at most `cap` chars without losing whole sections.

    Keeps the header (everything before the first '## ') and every '## ' heading. While over the
    cap, drops the last bullet/line of the currently longest section. Last resort (header alone too
    big): hard cut on a line boundary.
    """
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    parts = re.split(r"(?m)^(?=## )", text)
    head, sections = parts[0], [p.splitlines() for p in parts[1:]]

    def joined():
        return head + "".join("\n".join(s).rstrip("\n") + "\n\n" for s in sections)

    out = joined()
    while len(out) > cap:
        longest = max(sections, key=lambda s: len("\n".join(s)), default=None)
        if not longest or len(longest) <= 1:      # only headings left
            break
        longest.pop()
        while len(longest) > 1 and not longest[-1].strip():
            longest.pop()                         # drop blank tail lines too
        out = joined()
    if len(out) > cap:                            # header/headings alone exceed the cap
        out = out[:cap]
        out = out[:out.rfind("\n") + 1] if "\n" in out else out
    return out


def enforce_cap(path, cap, log=None):
    """Trim the file at `path` in place if it is over `cap`. Returns True if it changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    new = cap_text(text, cap)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    if log:
        log("learn", f"{path.name} trimmed from {len(text)} to {len(new)} chars (cap {cap})")
    return True
