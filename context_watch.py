"""Live assistant M3: in-RAM context tracker and local near-miss ('hint') detector. Pure, no I/O.

The user (10 Oct 2026, 23:18): spell checking is pointless, his apps already do it. What matters is subject
awareness: knowing what is on screen and spotting when what he types does not match it, e.g. a ticket
number with two digits swapped, a hostname one letter off, a different date from the one visible.

Everything here is rule-based and cheap (regex + edit distance), so it needs no model. A wrong hint is
worse than a missed one: anything ambiguous, short or already present on screen is never flagged.
"""
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, asdict

SNIP = 60

# ------------------------------------------------------------------ entity extraction
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DATE_NUM = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_TXT = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTHS})[a-z]*\.?\s+(\d{{4}})\b", re.I)
_HOST = re.compile(r"\b(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b"
                   r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+){2,}\b", re.I)
_ID = re.compile(r"(?<![\w.])#?(?=[A-Za-z]*\d)[A-Za-z]{0,6}\d{4,}(?![\w.])")
_MONEY = re.compile(r"£\s?\d[\d,]*(?:\.\d{2})?")
_NAME = re.compile(r"\b[A-Z][a-z]{2,}(?:[ \t]+[A-Z][a-z]{2,})+\b")   # never across a line break
_STOP = set("""the and for that with this from have will your you are was were but not they them then than
there their what when which while would could should about into over just like been being also some more
only other such very here where who how why all any can its it's our out has had his her she him please
thanks thank yeah okay ok""".split())
MONTH_NUM = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


@dataclass(frozen=True)
class Entity:
    kind: str       # id | host | ip | email | date | money | name
    value: str      # as seen
    norm: str       # normalised for comparing


def _norm_date(d, m, y):
    y = int(y)
    y = y + 2000 if y < 100 else y
    return f"{y:04d}-{int(m):02d}-{int(d):02d}"


def extract_entities(text):
    """Entities in `text`, longest/most specific first; overlapping matches are claimed once."""
    if not text:
        return []
    taken, out = [], []

    def claim(m, kind, norm):
        a, b = m.span()
        if any(a < y and x < b for x, y in taken):
            return
        taken.append((a, b))
        out.append(Entity(kind, m.group(0), norm))

    for m in _EMAIL.finditer(text):
        claim(m, "email", m.group(0).lower())
    for m in _IP.finditer(text):
        claim(m, "ip", m.group(0))
    for m in _DATE_NUM.finditer(text):
        g = m.groups()
        try:
            claim(m, "date", _norm_date(g[0], g[1], g[2]) if g[0] else _norm_date(g[5], g[4], g[3]))
        except ValueError:
            pass
    for m in _DATE_TXT.finditer(text):
        claim(m, "date", _norm_date(m.group(1), MONTH_NUM[m.group(2)[:3].lower()], m.group(3)))
    for m in _MONEY.finditer(text):
        claim(m, "money", re.sub(r"[£,\s]", "", m.group(0)))
    for m in _HOST.finditer(text):
        claim(m, "host", m.group(0).lower())
    for m in _ID.finditer(text):
        claim(m, "id", m.group(0).lstrip("#").upper())
    for m in _NAME.finditer(text):
        claim(m, "name", m.group(0).lower())
    return out


def keywords(text, n=8):
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']{3,}", text or "")]
    return [w for w, _ in Counter(w for w in words if w not in _STOP).most_common(n)]


# ------------------------------------------------------------------ near-miss maths
def osa(a, b):
    """Optimal string alignment distance plus whether the only edit was one adjacent swap."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _is_swap(a, b):
    if len(a) != len(b):
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def near_miss(typed, seen):
    """(kind_of_mismatch, confidence) if `typed` looks like a slip for `seen` (same entity kind), else None."""
    a, b = typed.norm, seen.norm
    if a == b or typed.kind != seen.kind:
        return None
    k = typed.kind
    if k == "date":
        ya, ma, da = a.split("-")
        yb, mb, db = b.split("-")
        if ya == yb and ma == db and da == mb:
            return "date_day_month_swapped", 0.8
        if osa(a, b) == 1:
            return "date_one_digit", 0.7
        return None
    if k in ("id", "ip", "money"):
        if len(a) < 4:
            return None
        if _is_swap(a, b):
            return f"{k}_digits_transposed", 0.9
        dist = osa(a, b)
        if dist == 1 and len(a) == len(b):
            return f"{k}_one_character", 0.75
        if dist == 1 and min(len(a), len(b)) >= 5:
            return f"{k}_digit_missing_or_extra", 0.7
        return None
    if k in ("host", "email"):
        if len(a) < 6:
            return None
        if _is_swap(a, b):
            return f"{k}_letters_transposed", 0.85
        if osa(a, b) == 1:
            return f"{k}_one_character", 0.8
        return None
    if k == "name":
        if len(a) < 6 or osa(a, b) != 1:
            return None
        return "name_one_letter", 0.7
    return None


# ------------------------------------------------------------------ tracker
@dataclass
class Hint:
    title: str
    typed: str
    entity: str
    kind: str
    confidence: float

    def as_dict(self):
        d = asdict(self)
        d["title"], d["typed"], d["entity"] = d["title"][:80], d["typed"][:SNIP], d["entity"][:SNIP]
        return d


class ContextTracker:
    """Rolling, RAM-only picture of the subject: entities seen per window (with age), keywords and a
    short topic history. observe() feeds screen/chat text; check_typed() compares what the user is typing."""

    def __init__(self, ttl_s=900, max_entities=400, threshold=0.7, clock=time.monotonic):
        self.ttl_s, self.max_entities, self.threshold, self.clock = ttl_s, max_entities, threshold, clock
        self._seen = {}                       # (kind, norm) -> [Entity, last_ts, titles set]
        self.topics = {}                      # title -> keywords
        self.history = deque(maxlen=20)       # (ts, title, keywords)
        self._hinted = deque(maxlen=200)

    def observe(self, title, text, source="screen"):
        now = self.clock()
        for e in extract_entities(text):
            rec = self._seen.get((e.kind, e.norm))
            if rec is None:
                self._seen[(e.kind, e.norm)] = [e, now, {title}]
            else:
                rec[1] = now
                rec[2].add(title)
        kw = keywords(text)
        if kw and self.topics.get(title) != kw:
            self.topics[title] = kw
            self.history.append((now, title, kw))
        self._expire(now)

    def _expire(self, now):
        for k in [k for k, r in self._seen.items() if now - r[1] > self.ttl_s]:
            del self._seen[k]
        if len(self._seen) > self.max_entities:
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1][1])[:len(self._seen) - self.max_entities]:
                del self._seen[k]

    def entities(self, kind=None):
        return [r[0] for r in self._seen.values() if kind is None or r[0].kind == kind]

    def clear(self):
        self._seen.clear()
        self.topics.clear()
        self.history.clear()

    def check_typed(self, title, typed_text, skip_last_partial=True):
        """Hints for entities in the user's typing that are a near-miss for exactly one entity in context."""
        text = typed_text or ""
        if skip_last_partial and text and not text[-1].isspace() and text[-1] not in ".,;:!?)":
            m = re.search(r"\S+$", text)
            text = text[:m.start()] if m else text       # the token still being typed is not finished
        self._expire(self.clock())
        typed_low = (typed_text or "").lower()
        hints = []
        for t in extract_entities(text):
            if (t.kind, t.norm) in self._seen:
                continue                                   # it matches something on screen exactly
            if any(e.norm == t.norm for e in self.entities()):
                continue
            close = [(near_miss(t, e), e) for e in self.entities(t.kind)]
            # self-match guard: a context entity that is itself a piece of what he typed (e.g. OCR of the
            # typing window's own title bar, which echoes the first few dozen typed characters) is his own
            # typing seen back, not something independently on screen. Never compare against it.
            close = [(r, e) for r, e in close if r and e.norm.lower() not in typed_low
                     and e.value.lower() not in typed_low]
            if len(close) != 1:
                continue                                   # nothing close, or ambiguous: stay quiet
            (kind, conf), e = close[0]
            if conf < self.threshold:
                continue
            key = (title, t.norm, e.norm)
            if key in self._hinted:
                continue
            self._hinted.append(key)
            hints.append(Hint(title, t.value, e.value, kind, conf))
        return hints
