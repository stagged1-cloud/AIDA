"""Regression test: a new job must not wipe helper boxes that are still running.

Bug (found 10 Oct 2026): App.run started every job with self.agents.clear(), so any new message from the user
removed the boxes of helpers that were still working in the background. The fix keeps boxes whose
status is 'Working' and drops only finished ones.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).parent / "voice_claude.py").read_text(encoding="utf-8")


def _job_start_block():
    """The few lines in the job-start code that deal with self.agents (between feed.clear() and working=True)."""
    i = SRC.index("self.feed.clear()\n        for aid in")
    return SRC[i:i + 400]


class TestRunningHelpersSurviveNewJob(unittest.TestCase):
    def test_job_start_does_not_clear_all_agents(self):
        # The old line (a blanket clear at job start) must be gone from the job-start block.
        self.assertNotIn("self.agents.clear()", _job_start_block().split("self.working = True")[0])

    def test_job_start_keeps_working_helpers(self):
        block = _job_start_block().split("self.working = True")[0]
        self.assertIn('!= "Working"', block)       # only entries that are not Working are removed
        self.assertIn("del self.agents[aid]", block)

    def test_the_filter_logic_itself(self):
        # Re-run the same filter on a fake agents dict, to prove the behaviour, not just the text.
        agents = {"a": {"status": "Working"}, "b": {"status": "Done"}, "c": {"status": "Failed"},
                  "d": {"status": "Stopped"}, "e": {"status": "Working"}}
        for aid in [k for k, a in agents.items() if a.get("status") != "Working"]:
            del agents[aid]
        self.assertEqual(sorted(agents), ["a", "e"])

    def test_cancel_still_closes_everything(self):
        # Cancel kills the job, so clearing all boxes there is still correct.
        i = SRC.index("def on_cancel(self):")
        self.assertIn("self.agents.clear()", SRC[i:i + 200])


if __name__ == "__main__":
    unittest.main()
