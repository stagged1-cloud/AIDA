"""Tests for context_watch.py (live assistant M3). Run on its own: .venv\\Scripts\\python.exe test_context_watch.py

Pure logic, no screen, no disk. The must-not-flag cases outnumber the true positives on purpose:
a wrong hint is worse than a missed one.
"""
import unittest

import context_watch as cw


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def kinds(text):
    return {(e.kind, e.norm) for e in cw.extract_entities(text)}


class TestEntityExtraction(unittest.TestCase):
    # E1: ticket-style IDs, with and without prefix or hash
    def test_ids(self):
        k = kinds("Ticket #48213 and INC0012345 raised")
        self.assertIn(("id", "48213"), k)
        self.assertIn(("id", "INC0012345"), k)

    # E2: hyphenated hostnames with digits and dotted FQDNs
    def test_hosts(self):
        k = kinds("Log on to GW-ACME-447 then files01.example.org")
        self.assertIn(("host", "gw-acme-447"), k)
        self.assertIn(("host", "files01.example.org"), k)

    # E3: UK dates in three spellings normalise to the same ISO value
    def test_dates_normalise(self):
        for t in ("10/10/2026", "10 Oct 2026", "2026-10-10", "10th October 2026"):
            self.assertIn(("date", "2026-10-10"), kinds(t), t)

    # E4: IPs, e-mail, money and multi-word names
    def test_other_kinds(self):
        k = kinds("Ping 10.0.4.12, mail liz.smith@example.co.uk about £1,250.00 for Ron Weasley")
        self.assertIn(("ip", "10.0.4.12"), k)
        self.assertIn(("email", "liz.smith@example.co.uk"), k)
        self.assertIn(("money", "1250.00"), k)
        self.assertIn(("name", "ron weasley"), k)

    # E5: an IP is not also read as a host or ID; a date is not read as an ID
    def test_no_double_claims(self):
        ents = cw.extract_entities("10.0.4.12 on 2026-10-10")
        self.assertEqual(sorted(e.kind for e in ents), ["date", "ip"])

    # E6: plain prose and short numbers give nothing
    def test_prose_empty(self):
        self.assertEqual(cw.extract_entities("I will ring you at 3 about the 12 files"), [])
        self.assertEqual(cw.extract_entities(""), [])

    # E7: keywords drop stop words
    def test_keywords(self):
        kw = cw.keywords("worklist worklist modality the the the and accession")
        self.assertEqual(kw[0], "worklist")
        self.assertNotIn("the", kw)


class TestNearMiss(unittest.TestCase):
    def setUp(self):
        self.t = cw.ContextTracker(clock=FakeClock())

    def hints(self, screen, typed, title="Zendesk"):
        self.t.observe(title, screen)
        return self.t.check_typed(title, typed + " ")

    # T1: transposed digits in a ticket number are flagged with high confidence
    def test_id_transposed(self):
        h = self.hints("Ticket #48213 is open", "Updated ticket 48123")
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0].kind, "id_digits_transposed")
        self.assertEqual(h[0].entity, "#48213")
        self.assertGreaterEqual(h[0].confidence, 0.9)

    # T2: hostname one character off
    def test_host_one_char(self):
        h = self.hints("Gateway GW-ACME-447 offline", "restarted GW-ACME-477")
        self.assertEqual([x.kind for x in h], ["host_one_character"])

    # T3: day and month swapped against the visible date
    def test_date_swap(self):
        h = self.hints("Appointment 03/10/2026", "booked for 10/03/2026")
        self.assertEqual([x.kind for x in h], ["date_day_month_swapped"])

    # T4: name one letter off
    def test_name(self):
        h = self.hints("Assigned to Alexandra Whitmore", "Hi Alexandra Whitmoore")
        self.assertEqual([x.kind for x in h], ["name_one_letter"])

    # T5: IP last octet transposed
    def test_ip(self):
        h = self.hints("Server 10.0.4.12", "ping 10.0.4.21")
        self.assertEqual([x.kind for x in h], ["ip_digits_transposed"])

    # N1: exact match on screen is never flagged
    def test_exact_not_flagged(self):
        self.assertEqual(self.hints("Ticket #48213", "Ticket 48213"), [])

    # N2: a completely different number is a different thing, not a slip
    def test_unrelated_number(self):
        self.assertEqual(self.hints("Ticket #48213", "see 91577"), [])

    # N3: two candidates equally close: ambiguous, stay quiet
    def test_ambiguous(self):
        self.assertEqual(self.hints("Tickets 48213 and 48132", "about 48123"), [])

    # N4: short IDs (under 4 characters) are never compared
    def test_short_ids(self):
        self.assertEqual(self.hints("Room 4821", "room 482"), [])

    # N5: different entity kinds never clash
    def test_kind_mismatch(self):
        self.assertEqual(self.hints("Server 10.0.4.12", "ticket 10041"), [])

    # N6: the token still being typed is ignored until it is finished
    def test_partial_token(self):
        self.t.observe("Z", "Ticket #48213")
        self.assertEqual(self.t.check_typed("Z", "ticket 4812"), [])
        self.assertEqual(self.t.check_typed("Z", "ticket 48123"), [])

    # N7: nothing on screen means nothing to clash with
    def test_empty_context(self):
        self.assertEqual(self.t.check_typed("Z", "ticket 48123 "), [])

    # N8: same hint is logged once per window, not on every keystroke
    def test_dedupe(self):
        self.assertEqual(len(self.hints("Ticket #48213", "ticket 48123")), 1)
        self.assertEqual(self.t.check_typed("Zendesk", "ticket 48123 again "), [])

    # N9: a short hostname one letter off is not flagged (too likely to be another real box)
    def test_short_host(self):
        self.assertEqual(self.hints("box a-1", "box a-2"), [])

    # N10: two-letter-different names are not slips
    def test_name_far(self):
        self.assertEqual(self.hints("Assigned to Ron Smith", "Thanks Rob Smyth"), [])

    # N11: dates far apart are just different dates
    def test_date_far(self):
        self.assertEqual(self.hints("Due 03/10/2026", "next review 17/11/2026"), [])

    # N12: something typed that also appears anywhere in context (other window) is fine
    def test_seen_elsewhere(self):
        self.t.observe("Teams", "Ticket 48123 is the other one")
        self.assertEqual(self.hints("Ticket #48213", "ticket 48123"), [])

    # N13: entities expire after the TTL
    def test_expiry(self):
        clock = FakeClock()
        t = cw.ContextTracker(ttl_s=60, clock=clock)
        t.observe("Z", "Ticket #48213")
        clock.t += 61
        self.assertEqual(t.check_typed("Z", "ticket 48123 "), [])

    # N14: hints below the threshold are dropped
    def test_threshold(self):
        t = cw.ContextTracker(threshold=0.95, clock=FakeClock())
        t.observe("Z", "Ticket #48213")
        self.assertEqual(t.check_typed("Z", "ticket 48123 "), [])

    # H1: hint fields are capped at 60 characters
    def test_hint_caps(self):
        d = cw.Hint("t" * 200, "x" * 200, "y" * 200, "id", 0.9).as_dict()
        self.assertEqual((len(d["title"]), len(d["typed"]), len(d["entity"])), (80, 60, 60))

    # H2: topic history records a change of subject per window
    def test_topic_history(self):
        self.t.observe("A", "modality worklist worklist accession")
        self.t.observe("A", "invoice invoice payment overdue")
        self.assertEqual(len(self.t.history), 2)
        self.assertEqual(self.t.topics["A"][0], "invoice")


class TestLiveTestRegression(unittest.TestCase):
    """The 10 Oct live test: reference in window A, near-misses typed in window B."""
    REF = ("Ticket INC-482917 raised on server GW-LAB-447 (10.20.30.41) on 11/10/2026 "
           "for customer Northgate Imaging")

    def setUp(self):
        self.t = cw.ContextTracker(clock=FakeClock())
        self.t.observe("A - Notepad", self.REF)

    def one(self, typed):
        hs = self.t.check_typed("B - Notepad", typed)
        self.assertEqual(len(hs), 1, hs)
        return hs[0]

    # R1: ticket ID with two digits swapped (INC-482917 -> INC-482971)
    def test_r1_ticket_digit_swap(self):
        h = self.one("check ticket INC-482971 .")
        self.assertEqual((h.entity, h.kind), ("INC-482917", "host_letters_transposed"))

    # R2: hostname with two characters transposed (GW-LAB-447 -> GW-LAB-474)
    def test_r2_host_transposed(self):
        self.assertEqual(self.one("server GW-LAB-474 .").entity, "GW-LAB-447")

    # R2b: hostname one character off (GW-LAB-447 -> GW-LAB-441)
    def test_r2b_host_one_char(self):
        self.assertEqual(self.one("server GW-LAB-441 .").kind, "host_one_character")

    # R3: multi-word name with one letter dropped ('Imagng')
    def test_r3_name_letter_missing(self):
        h = self.one("for Northgate Imagng .")
        self.assertEqual((h.entity, h.kind), ("Northgate Imaging", "name_one_letter"))

    # R3b: a name at the end of one UIA control is not glued to the next control's first word
    def test_r3b_name_not_joined_across_lines(self):
        t = cw.ContextTracker(clock=FakeClock())
        t.observe("A", "for customer Northgate Imaging\nTicket INC-482917 raised")
        self.assertEqual(t.check_typed("B", "for Northgate Imagng .")[0].entity, "Northgate Imaging")

    # R4: exact values stay quiet
    def test_r4_exact_quiet(self):
        self.assertEqual(self.t.check_typed("B", "INC-482917 GW-LAB-447 Northgate Imaging ."), [])

    # R5: self-match guard: an echo of his own typing (title-bar OCR, 'NC-482971') is never the reference
    def test_r5_self_echo_ignored(self):
        t = cw.ContextTracker(clock=FakeClock())
        t.observe("B title", "*Please check ticket NC-482971 on s - Notepad")
        self.assertEqual(t.check_typed("B", "Please check ticket INC-482971 on server ."), [])

    # R6: the self-echo does not make a real near-miss ambiguous
    def test_r6_echo_does_not_block_real_hint(self):
        self.t.observe("B title", "*Please check ticket NC-482971 on s - Notepad")
        self.assertEqual(self.one("Please check ticket INC-482971 .").entity, "INC-482917")


if __name__ == "__main__":
    unittest.main(verbosity=2)
