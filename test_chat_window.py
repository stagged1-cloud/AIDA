"""Tests for chat_window.py (no window needed). Run: .venv\\Scripts\\python -m unittest test_chat_window -v"""
import datetime
import pathlib
import re
import shutil
import tempfile
import unittest

import chat_window as cw


class Clock:
    pass


class FakeImage:
    """Stands in for a PIL image: records what save() was asked to do."""
    def __init__(self):
        self.saved = []

    def save(self, path, fmt=None):
        pathlib.Path(path).write_bytes(b"\x89PNG fake")
        self.saved.append((str(path), fmt))


class Roles(unittest.TestCase):
    def test_role_mapping(self):
        # each log label lands in the right visual bucket
        for who, role in [("You", "you"), ("You (phone)", "you"), ("You (text)", "you"), ("Claude", "aida"),
                          ("agent", "helper"), ("step", "step"), ("note", "step"), ("error", "error"),
                          ("system", "system"), ("setup", "system"), ("watcher", "system")]:
            self.assertEqual(cw.role_of(who), role, who)

    def test_user_and_aida_get_header_and_body(self):
        segs = cw.format_entry("12:00:01", "You", "hello\nthere")
        tags = [t for _, t in segs]
        self.assertIn("you_h", tags)
        self.assertEqual(segs[-1], ("hello\nthere\n", "you"))        # full multi-line body kept
        self.assertEqual(segs[1][0], "YOU")

    def test_typed_turn_header_says_text(self):
        segs = cw.format_entry("12:00:01", "You (text)", "hi")
        self.assertEqual(segs[1][0], "YOU (text)")
        self.assertEqual(cw.format_entry("12:00:01", "Claude", "yo")[1][0], "AIDA")

    def test_system_line_is_one_clipped_line(self):
        long = "x" * 1000
        segs = cw.format_entry("12:00:01", "step", "a\nb " + long, max_line=50)
        body = segs[-1][0]
        self.assertTrue(body.endswith("...\n"))
        self.assertEqual(body.count("\n"), 1)           # newlines collapsed
        self.assertLessEqual(len(body), 51)

    def test_empty_and_odd_input(self):
        self.assertTrue(cw.format_entry("t", "system", ""))
        self.assertTrue(cw.format_entry("t", None, None))


class Mirror(unittest.TestCase):
    def test_feed_is_ordered_and_incremental(self):
        # the window polls entries_since(last_seq): it must see each line exactly once and in order
        start = cw.entries_since(0)
        base = start[-1][0] if start else 0
        cw.feed("1", "You", "one")
        cw.feed("2", "Claude", "two")
        new = cw.entries_since(base)
        self.assertEqual([e[3] for e in new], ["one", "two"])
        self.assertEqual(cw.entries_since(new[-1][0]), [])

    def test_feed_never_raises(self):
        cw.feed(None, object(), None)

    def test_voice_claude_calls_feed_from_show(self):
        # the hook really is wired into show(): checked on the source, voice_claude is not imported (it loads models)
        src = (pathlib.Path(cw.__file__).parent / "voice_claude.py").read_text(encoding="utf-8")
        body = src[src.index("def show(who, text):"):src.index("def log(who, text):")]
        self.assertIn("chat_window.feed(t, cw_who, text)", body)


class Attach(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def f(self, name, data=b"abc"):
        p = self.d / name
        p.write_bytes(data)
        return str(p)

    def test_add_and_remove(self):
        a = cw.Attachments()
        ok, msg = a.add(self.f("a.txt"))
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.assertEqual(a.items[0]["name"], "a.txt")
        self.assertEqual(a.items[0]["size"], 3)
        self.assertTrue(a.remove(0))
        self.assertEqual(len(a), 0)
        self.assertFalse(a.remove(5))                    # out of range is harmless

    def test_remove_by_path(self):
        a = cw.Attachments()
        p = self.f("a.txt")
        a.add(p)
        self.assertTrue(a.remove(p))
        self.assertEqual(len(a), 0)

    def test_duplicates_rejected_case_insensitively(self):
        a = cw.Attachments()
        p = self.f("a.txt")
        self.assertTrue(a.add(p)[0])
        ok, msg = a.add(p.upper())                       # Windows paths are case-insensitive
        self.assertFalse(ok)
        self.assertIn("already", msg)

    def test_missing_file(self):
        ok, msg = cw.Attachments().add(str(self.d / "nope.txt"))
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_size_cap_warn_and_hard(self):
        a = cw.Attachments(warn=100, hard=1000)
        ok, msg = a.add("big.bin", size=500)             # explicit size: no disk access
        self.assertTrue(ok)
        self.assertIn("large", msg)
        self.assertTrue(a.items[0]["big"])
        ok, msg = a.add("huge.bin", size=5000)
        self.assertFalse(ok)
        self.assertIn("too big", msg)
        ok, msg = a.add("small.bin", size=10)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_file_count_limit(self):
        a = cw.Attachments(max_files=2)
        a.add("a", size=1)
        a.add("b", size=1)
        ok, msg = a.add("c", size=1)
        self.assertFalse(ok)
        self.assertIn("limit", msg)

    def test_folder_accepted(self):
        ok, _ = cw.Attachments().add(str(self.d))
        self.assertTrue(ok)

    def test_human_size(self):
        self.assertEqual(cw.human_size(0), "0 B")
        self.assertEqual(cw.human_size(2048), "2.0 KB")
        self.assertEqual(cw.human_size(30 * 1024 * 1024), "30.0 MB")

    def test_message_includes_paths_and_data_warning(self):
        a = cw.Attachments()
        a.add("C:\\x\\shot.png", size=2048)
        m = cw.build_message("what is this", a.items)
        self.assertTrue(m.startswith("what is this"))
        self.assertIn("C:\\x\\shot.png", m)
        self.assertIn("untrusted data", m)
        self.assertIn("not instructions", m)
        self.assertIn("memory/topics", m)                # told never to file attachments into synced memory

    def test_message_without_attachments_is_unchanged(self):
        self.assertEqual(cw.build_message("  hello ", []), "hello")

    def test_attachments_without_text(self):
        a = cw.Attachments()
        a.add("f.txt", size=1)
        self.assertIn("f.txt", cw.build_message("", a.items))
        self.assertEqual(cw.display_text("", a.items), "[attached: f.txt]")


class Clipboard(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_classify(self):
        self.assertEqual(cw.classify_clipboard(None), ("none", None))
        self.assertEqual(cw.classify_clipboard(["a", "b"]), ("files", ["a", "b"]))
        img = FakeImage()
        self.assertEqual(cw.classify_clipboard(img), ("image", img))
        self.assertEqual(cw.classify_clipboard("just text"), ("none", None))

    def test_image_saved_as_png_in_inbox(self):
        inbox = self.d / "state" / "inbox"              # does not exist yet: must be created
        img = FakeImage()
        p = cw.save_clipboard_image(img, inbox, datetime.datetime(2026, 10, 10, 17, 45, 3))
        self.assertEqual(p.name, "clip-20261010-174503.png")
        self.assertTrue(p.exists())
        self.assertEqual(img.saved[0][1], "PNG")
        self.assertEqual(p.parent, inbox)

    def test_never_overwrites(self):
        inbox = self.d / "i"
        t = datetime.datetime(2026, 1, 1, 0, 0, 0)
        p1 = cw.save_clipboard_image(FakeImage(), inbox, t)
        p2 = cw.save_clipboard_image(FakeImage(), inbox, t)
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.exists() and p2.exists())

    def test_default_inbox_is_local_state_not_memory(self):
        parts = [x.lower() for x in cw.INBOX.parts]
        self.assertEqual(parts[-2:], ["state", "inbox"])
        self.assertNotIn("memory", parts)
        gi = (pathlib.Path(cw.__file__).parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("state/inbox/", gi)                # never committed


class Sending(unittest.TestCase):
    def setUp(self):
        self.sent, self.notes, self.busy = [], [], False
        self.d = cw.Dispatcher(lambda *a: self.sent.append(a), lambda: self.busy, self.notes.append)

    def test_idle_sends_straight_away(self):
        self.assertEqual(self.d.submit("hello there"), "sent")
        self.assertEqual(self.sent, [("hello there", "hello there", True)])

    def test_empty_is_ignored(self):
        self.assertEqual(self.d.submit("   "), "empty")
        self.assertEqual(self.sent, [])

    def test_busy_queues_then_pumps_in_order(self):
        self.busy = True
        self.assertEqual(self.d.submit("first"), "queued")
        self.assertEqual(self.d.submit("second"), "queued")
        self.assertEqual(self.sent, [])
        self.assertTrue(self.notes)                      # The user is told it is waiting
        self.assertFalse(self.d.pump())                  # still busy
        self.busy = False
        self.assertTrue(self.d.pump())
        self.assertEqual(self.sent[0][0], "first")
        self.assertTrue(self.d.pump())
        self.assertEqual(self.sent[1][0], "second")
        self.assertFalse(self.d.pump())

    def test_speak_flag_passed_through(self):
        self.d.submit("quiet please", speak=False)
        self.assertFalse(self.sent[0][2])

    def test_attachments_go_in_message_not_in_shown_text(self):
        a = cw.Attachments()
        a.add("C:\\x\\a.png", size=10)
        self.d.submit("look", a.items)
        shown, message, _ = self.sent[0]
        self.assertIn("[attached: a.png]", shown)
        self.assertNotIn("untrusted", shown)
        self.assertIn("C:\\x\\a.png", message)

    def test_attachment_only_turn_is_allowed(self):
        a = cw.Attachments()
        a.add("f", size=1)
        self.assertEqual(self.d.submit("", a.items), "sent")

    def test_command_words_pass_through_when_busy(self):
        # stop / cancel / confirmed must not sit in a queue behind the job they are about to affect
        self.busy = True
        for word in ("stop", "Cancel", "okay, shut up", "confirmed", "Confirmed.", "yes, go ahead", "repeat that"):
            self.assertEqual(self.d.submit(word), "sent", word)
        self.assertEqual(len(self.d.queue), 0)

    def test_ordinary_text_that_merely_contains_a_command_word_queues(self):
        self.busy = True
        for text in ("please do not stop the backup", "I have not confirmed anything", "cancel my order of pizza"):
            self.assertEqual(self.d.submit(text), "queued", text)

    def test_confirm_regex_matches_the_apps_own(self):
        # typed 'confirmed' must be judged by the same words as the spoken one: compare against voice_claude.py
        src = (pathlib.Path(cw.__file__).parent / "voice_claude.py").read_text(encoding="utf-8")
        m = re.search(r'^CONFIRM_RE = re\.compile\(r"(.*)", re\.I\)', src, re.M)
        self.assertIsNotNone(m)
        app_re = re.compile(m.group(1), re.I)
        for t in ("confirmed", "Yes, confirmed", "go ahead", "do it", "delete it", "approve", "nope", "confirmation",
                  "stop", "cancel"):
            self.assertEqual(bool(app_re.match(t)), bool(cw.CONFIRM_LIKE_RE.match(t)), t)

    def test_text_turn_hook_exists_and_uses_the_guard_token(self):
        src = (pathlib.Path(cw.__file__).parent / "voice_claude.py").read_text(encoding="utf-8")
        body = src[src.index("    def text_turn("):src.index("    def phone_ask(")]
        self.assertIn("CONFIRM_RE.match(text.strip())", body)
        self.assertIn("TOKEN_FILE.write_text", body)
        self.assertIn('log(USER + " (text)"', body)           # marked as from the text window


class Geometry(unittest.TestCase):
    def test_normal_geometry_kept(self):
        self.assertEqual(cw.clamp_geometry(640, 720, 100, 50, 1920, 1080), (640, 720, 100, 50))

    def test_off_screen_position_pulled_back(self):
        w, h, x, y = cw.clamp_geometry(640, 720, 5000, -300, 1920, 1080)
        self.assertEqual((x, y), (1920 - 640, 0))

    def test_too_small_or_too_big_size(self):
        w, h, x, y = cw.clamp_geometry(10, 10, 0, 0, 1920, 1080)
        self.assertEqual((w, h), (420, 320))
        w, h, x, y = cw.clamp_geometry(9999, 9999, 0, 0, 1920, 1080)
        self.assertEqual((w, h), (1920, 1080))

    def test_clip(self):
        self.assertEqual(cw.clip("a  b\nc", 10), "a b c")
        self.assertEqual(len(cw.clip("x" * 100, 10)), 10)


class Palette(unittest.TestCase):
    """You 18:15: AIDA blue, the user green, errors red, everything else as before."""
    def test_aida_is_blue_and_user_is_green(self):
        def hue(h):                                   # crude: which channel dominates
            r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
            return "r" if r > g and r > b else "g" if g >= r and g > b else "b"
        for col in cw.PALETTE["aida"]:
            self.assertEqual(hue(col), "b", col)       # blue
        for col in cw.PALETTE["you"]:
            self.assertEqual(hue(col), "g", col)       # green
        for col in cw.PALETTE["error"]:
            self.assertEqual(hue(col), "r", col)       # red

    def test_unchanged_roles_keep_their_colours(self):
        self.assertEqual(cw.PALETTE["helper"], ("#A07CF8", "#A99BD6"))
        self.assertEqual(cw.PALETTE["system"], ("#D9A93A", "#BFA15A"))
        self.assertEqual(cw.PALETTE["step"], (cw.DIM, cw.DIM))

    def test_role_colours_alias_follows_palette(self):
        self.assertIs(cw.ROLE_COLOURS["aida"], cw.PALETTE["aida"])

    def test_errors_are_red_by_label_and_by_text(self):
        # label 'error' (microphone failures), helper that failed, the guard block, a failed sync
        self.assertEqual(cw.role_of("error", "Microphone failed: device busy"), "error")
        self.assertEqual(cw.role_of("agent", "Chat window failed: timeout"), "error")
        self.assertEqual(cw.role_of("system", "Blocked by the safety check until you say 'confirmed'."), "error")
        self.assertEqual(cw.role_of("sync", "pull failed, skipping this round"), "error")

    def test_normal_lines_are_not_red(self):
        self.assertEqual(cw.role_of("agent", "Started: Chat window"), "helper")
        self.assertEqual(cw.role_of("system", "Assistant started"), "system")
        self.assertEqual(cw.role_of("Claude", "That failed, sorry"), "aida")     # conversation is never recoloured
        self.assertEqual(cw.role_of("You", "it failed"), "you")

    def test_error_entry_uses_error_tags(self):
        tags = [t for _, t in cw.format_entry("t", "system", "Blocked by the safety check")]
        self.assertIn("error_h", tags)
        self.assertIn("error", tags)


class Markdown(unittest.TestCase):
    """Display-only markdown for AIDA replies; speech and logs keep the raw text."""
    def flat(self, text):
        return "".join(t for t, _ in cw.md_segments(text, "aida"))

    def test_bold_gets_bold_tag_and_no_asterisks(self):
        segs = cw.md_segments("This is **very** good", "aida")
        self.assertIn(("very", "aida_b"), segs)
        self.assertNotIn("*", self.flat("This is **very** good"))

    def test_inline_code_is_monospace_tag(self):
        self.assertIn(("run.bat", "aida_c"), cw.md_segments("Open `run.bat` now", "aida"))

    def test_bullets_become_glyph(self):
        out = self.flat("- one\n* two\n  - nested")
        self.assertEqual(out, "\u2022 one\n\u2022 two\n  \u2022 nested\n")

    def test_headings_are_plain(self):
        self.assertEqual(self.flat("## Result"), "Result\n")

    def test_code_fence_lines_dropped_content_kept(self):
        segs = cw.md_segments("a\n```py\nx = 1\n```\nb", "aida")
        self.assertIn(("x = 1\n", "aida_c"), segs)
        self.assertNotIn("```", self.flat("a\n```py\nx = 1\n```\nb"))

    def test_links_keep_label_only(self):
        self.assertEqual(self.flat("see [the docs](http://x.y/z)"), "see the docs\n")

    def test_italics_stripped_but_maths_and_lone_stars_kept(self):
        self.assertEqual(self.flat("an *odd* day"), "an odd day\n")
        self.assertEqual(self.flat("2*3*4 and a * b"), "2*3*4 and a * b\n")

    def test_plain_text_is_one_segment_ending_in_newline(self):
        self.assertEqual(cw.md_segments("hello there", "aida"), [("hello there\n", "aida")])

    def test_empty_and_unclosed_markers_do_not_crash(self):
        self.assertTrue(cw.md_segments("", "aida"))
        self.assertEqual(self.flat("an **unclosed bold"), "an **unclosed bold\n")
        self.assertEqual(self.flat("one `tick"), "one `tick\n")

    def test_only_aida_is_rendered(self):
        # the user's own text is shown exactly as typed
        self.assertEqual(cw.format_entry("t", "You", "**x**")[-1], ("**x**\n", "you"))
        aida = cw.format_entry("t", "Claude", "**x**")
        self.assertIn(("x", "aida_b"), aida)
        self.assertNotIn("**", "".join(t for t, _ in aida))


class Switches(unittest.TestCase):
    """SwitchModel: the state behind the Head / Speak replies / Always on top toggles."""
    def test_labels_and_tooltips(self):
        self.assertEqual(cw.SWITCH_LABELS, {"head": "Head", "speak": "Speak replies", "top": "Always on top"})
        self.assertEqual(cw.SWITCH_TIPS["top"], "Keeps this window above all other windows")
        self.assertEqual(cw.SWITCH_TIPS["head"], "Show or hide the animated head")
        self.assertEqual(cw.SWITCH_TIPS["speak"], "Read typed replies aloud")

    def test_model_on_off_and_knob_position(self):
        m = cw.SwitchModel("Head", "tip", on=False)
        self.assertEqual(m.knob_pos(), 0.0)           # off = knob left
        self.assertTrue(m.set(True))
        self.assertEqual(m.knob_pos(), 1.0)           # on = knob right (position, not just colour)
        self.assertFalse(m.set(True))                 # no change reported
        self.assertEqual(m.aria(), "Head: on")

    def test_click_calls_command_but_does_not_flip_by_itself(self):
        calls = []
        m = cw.SwitchModel("Head", "tip", False, lambda: calls.append(1))
        m.activate()
        self.assertEqual(calls, [1])
        self.assertFalse(m.on)                         # the owner decides, then calls set()

    def test_keyboard_space_and_enter_toggle_others_ignored(self):
        calls = []
        m = cw.SwitchModel("x", "t", False, lambda: calls.append(1))
        for k in ("space", "Return", "KP_Enter"):
            self.assertTrue(m.key(k))
        self.assertFalse(m.key("a"))
        self.assertFalse(m.key("Tab"))
        self.assertEqual(len(calls), 3)

    def test_state_sync_from_voice_command_and_persistence(self):
        # 'head off' by voice -> controller -> head_changed -> model.set(False); start-up restores saved state
        m = cw.SwitchModel("Head", "t", on=True)       # restored from state.json
        self.assertEqual(m.knob_pos(), 1.0)
        m.set(False)                                   # voice command arrives
        self.assertEqual(m.aria(), "Head: off")

    def test_colours_differ_between_on_and_off(self):
        self.assertNotEqual(cw.SWITCH_ON, cw.SWITCH_OFF)



class HotkeyMenuAndDockColours(unittest.TestCase):
    def test_dock_background_is_the_window_background(self):
        # the head blends over the window colour: the dock must not use the lighter PANEL or the EDGE colour
        import head_dock
        import inspect
        self.assertNotEqual(cw.BG, cw.PANEL)
        self.assertIn("self.bg = self.PANEL = BG", inspect.getsource(head_dock.DockPanel))

    def test_log_pane_and_shell_share_the_dock_colour(self):
        src = pathlib.Path(cw.__file__).read_text(encoding="utf-8")
        self.assertIn("self.shell = w = tk.Frame(self.win, bg=BG)", src)
        self.assertIn("self.text = tk.Text(mid, wrap=\"word\", bg=BG", src)

    def test_old_collapsed_flag_is_dropped_not_obeyed(self):
        # with no collapse header, a stale saved 'collapsed' must not hide the head
        src = pathlib.Path(cw.__file__).read_text(encoding="utf-8")
        self.assertIn('self.state.pop("head_dock_collapsed", None)', src)
        self.assertIn("DockController(self.dock, False, remember)", src)

    def test_menu_strip_only_with_hotkey_api(self):
        import hotkey_ui
        self.assertFalse(hotkey_ui.app_supports(object()))


if __name__ == "__main__":
    unittest.main()
