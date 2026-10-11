"""Tests for the restart countdown (pure parts in chat_window.py)."""
import unittest
import chat_window as cw


class Countdown(unittest.TestCase):
    def test_format_full(self):                      # 30:00 at start
        self.assertEqual(cw.fmt_countdown(1800), "30:00")

    def test_format_padding_and_round_up(self):      # 59.2s shows 01:00? no: ceil -> 00:60 avoided
        self.assertEqual(cw.fmt_countdown(59.2), "01:00")
        self.assertEqual(cw.fmt_countdown(5), "00:05")

    def test_format_negative_clamps(self):           # never shows negative
        self.assertEqual(cw.fmt_countdown(-3), "00:00")

    def test_state_running(self):
        self.assertEqual(cw.countdown_state(1000, 400), ("running", 600))

    def test_state_due_boundary_is_overdue(self):    # exactly at due = reminder fires
        self.assertEqual(cw.countdown_state(1000, 1000), ("overdue", 0))

    def test_state_overdue(self):
        self.assertEqual(cw.countdown_state(1000, 5000), ("overdue", 0))

    def test_hidden_by_config(self):
        self.assertEqual(cw.countdown_state(1000, 400, enabled=False)[0], "hidden")
        self.assertIsNone(cw.countdown_look(1000, 400, enabled=False))

    def test_hidden_until_due_known(self):           # loop has not started yet
        self.assertIsNone(cw.countdown_look(None, 400))

    def test_flash_toggles_with_injected_clock(self):
        self.assertTrue(cw.flash_on(100.2))
        self.assertFalse(cw.flash_on(101.2))
        self.assertTrue(cw.flash_on(102.9))

    def test_look_running_text(self):
        self.assertEqual(cw.countdown_look(1800, 0)[0], "Restart in 30:00")

    def test_look_overdue_alternates_red_and_dim(self):
        a = cw.countdown_look(0, 100.5)
        b = cw.countdown_look(0, 101.5)
        self.assertEqual(a[0], "Restart now")
        self.assertEqual(a[1], "#E5484D")
        self.assertNotEqual(a[1], b[1])


if __name__ == "__main__":
    unittest.main()
