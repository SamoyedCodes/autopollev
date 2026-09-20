"""Regression tests for CLI-only paths (--cli / --auto / --history)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from autopollev.auth import CookieExpiredError
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


class RecaptureOnExpiryTests(unittest.TestCase):
    """The CLI used to send the user to DevTools; it re-captures now."""

    def setUp(self):
        self.config = Mock()
        self.config.cookies = {"polleverywhere_session_id": "fresh"}
        self.auth = Mock()

    def test_saves_and_swaps_in_the_captured_cookie(self):
        with patch("autopollev.session_capture.capture_session_id",
                   return_value={"polleverywhere_session_id": "fresh"}) as capture:
            self.assertTrue(main._recapture_session(self.config, self.auth))

        capture.assert_called_once()  # the silent attempt was enough
        self.config.update_cookie.assert_called_once_with(
            "polleverywhere_session_id", "fresh"
        )
        self.auth.refresh_session.assert_called_once_with(self.config.cookies)

    def test_falls_back_to_a_login_window_when_the_silent_try_fails(self):
        with patch("autopollev.session_capture.capture_session_id",
                   side_effect=[None, {"polleverywhere_session_id": "fresh"}]) as capture:
            self.assertTrue(main._recapture_session(self.config, self.auth))

        self.assertTrue(capture.call_args_list[0].kwargs["headless"])
        self.assertFalse(capture.call_args_list[1].kwargs["headless"])

    def test_reports_failure_when_nothing_is_captured(self):
        with patch("autopollev.session_capture.capture_session_id",
                   return_value=None):
            self.assertFalse(main._recapture_session(self.config, self.auth))
        self.auth.refresh_session.assert_not_called()

    def test_reports_failure_when_the_new_cookie_does_not_validate(self):
        self.auth.validate_cookie.side_effect = CookieExpiredError("still dead")
        with patch("autopollev.session_capture.capture_session_id",
                   return_value={"polleverywhere_session_id": "fresh"}):
            self.assertFalse(main._recapture_session(self.config, self.auth))


if __name__ == "__main__":
    unittest.main()
