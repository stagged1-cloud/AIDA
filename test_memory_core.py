"""Tests for memory_core.py (cap on the always-loaded memory files). Run: python -m pytest test_memory_core.py -q"""
import pathlib

import memory_core as mc

PROFILE = ("# How you talk\nheader line\n\n"
           "## Vocabulary\nmate, bloody, bollocks\n\n"
           "## Phrasing habits\n" + "".join(f"- habit number {i} with some padding text\n" for i in range(200)) + "\n"
           "## Transcription quirks\n- Confound = confirmed\n")


# T1: text already under the cap is returned unchanged.
def test_under_cap_unchanged():
    assert mc.cap_text("short", 100) == "short"


# T2: over-cap text comes back at or under the cap.
def test_over_cap_trimmed_to_cap():
    out = mc.cap_text(PROFILE, 1500)
    assert len(out) <= 1500


# T3: trimming keeps the header and every section heading (no whole section lost).
def test_headings_survive():
    out = mc.cap_text(PROFILE, 1500)
    for h in ("# How you talk", "## Vocabulary", "## Phrasing habits", "## Transcription quirks"):
        assert h in out


# T4: the longest section is trimmed first; small sections keep their content.
def test_longest_section_trimmed_first():
    out = mc.cap_text(PROFILE, 1500)
    assert "mate, bloody, bollocks" in out
    assert "- Confound = confirmed" in out
    assert "habit number 0 " in out           # earliest bullets kept
    assert "habit number 199" not in out      # latest bullets dropped


# T5: edge case, cap of zero or less returns empty string.
def test_zero_cap():
    assert mc.cap_text(PROFILE, 0) == ""
    assert mc.cap_text(PROFILE, -5) == ""


# T6: edge case, header alone bigger than cap: hard cut on a line boundary, still within cap.
def test_huge_header_hard_cut():
    text = "".join(f"header line {i}\n" for i in range(100))
    out = mc.cap_text(text, 200)
    assert len(out) <= 200 and out.endswith("\n")


# T7: enforce_cap rewrites an oversized file in place and reports it.
def test_enforce_cap_rewrites(tmp_path):
    f = tmp_path / "how_you_talk.md"
    f.write_text(PROFILE, encoding="utf-8")
    logged = []
    assert mc.enforce_cap(f, 1500, log=lambda *a: logged.append(a)) is True
    assert len(f.read_text(encoding="utf-8")) <= 1500
    assert logged


# T8: enforce_cap leaves a small file untouched.
def test_enforce_cap_noop(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("tiny\n", encoding="utf-8")
    assert mc.enforce_cap(f, 1500) is False
    assert f.read_text(encoding="utf-8") == "tiny\n"


# T9: missing file is not an error.
def test_enforce_cap_missing_file(tmp_path):
    assert mc.enforce_cap(tmp_path / "nope.md", 100) is False


# T10: the real loaded files are within their caps right now.
def test_real_loaded_files_within_caps():
    topics = pathlib.Path(__file__).parent / "assistant" / "memory" / "topics"
    assert len((topics / "how_you_talk.md").read_text(encoding="utf-8")) <= mc.PROFILE_CAP
    assert len((topics / "core_rules.md").read_text(encoding="utf-8")) <= mc.RULES_CAP
    assert (topics / "full" / "how_you_talk.md").exists()      # full profile archived, not lost
