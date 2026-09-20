"""Regression tests for CLI-only paths (--cli / --auto / --history)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopollev.logger import VoteHistoryLogger
from autopollev.voter import Voter


def make_voter():
    return Voter(Mock(), Mock())


class CountdownWithoutTerminalTests(unittest.TestCase):
    """Putting a non-terminal stdin in cbreak mode raises and lost the vote."""

    def test_piped_stdin_waits_instead_of_raising(self):
        voter = make_voter()
        read_fd, _write_fd = os.pipe()
        self.addCleanup(os.close, _write_fd)
        piped = os.fdopen(read_fd)
        self.addCleanup(piped.close)

        with patch.object(sys, "stdin", piped):
            self.assertIsNone(voter._input_with_timeout(0.2, 3))

    def test_missing_stdin_waits_instead_of_raising(self):
        voter = make_voter()
        with patch.object(sys, "stdin", None):
            self.assertIsNone(voter._input_with_timeout(0.2, 3))

    def test_stop_request_cuts_the_wait_short(self):
        voter = make_voter()
        with patch.object(sys, "stdin", None):
            self.assertIsNone(
                voter._input_with_timeout(30, 3, should_stop=lambda: True)
            )


class AbortDuringCountdownTests(unittest.TestCase):
    """Ctrl-C during the prompt ran to timeout and then voted anyway."""

    def test_no_vote_is_submitted_after_a_stop_request(self):
        voter = make_voter()
        voter.get_poll_options = Mock(return_value={
            "title": "Q", "state": "opened",
            "options": [{"id": 1, "value": "A"}, {"id": 2, "value": "B"}],
        })
        voter._submit_vote = Mock()
        voter._input_with_timeout = Mock(return_value=None)

        with patch("autopollev.voter.notify_new_poll"):
            result = voter.interactive_vote("uid", timeout=1,
                                            should_stop=lambda: True)

        self.assertIsNone(result)
        voter._submit_vote.assert_not_called()


class TornHistoryLineTests(unittest.TestCase):
    """A crash mid-append left a partial line that sank --history entirely."""

    def test_partial_last_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            history = VoteHistoryLogger(directory)
            history.record_vote("uid", "Q", "A", 1, "success", 200)
            with open(history.log_file, "a", encoding="utf-8") as handle:
                handle.write('{"timestamp": "2026-09-2')

            records = history.get_history()
            self.assertEqual(len(records), 1)
            self.assertEqual(history.get_stats()["success"], 1)


if __name__ == "__main__":
    unittest.main()
