"""Regression tests for GUI hostname restart event handling."""

import queue
import unittest
from unittest.mock import Mock, patch

from autopollev.gui import AutoPollEvGUI


class HostRestartEventTests(unittest.TestCase):
    def test_discards_old_monitor_events_before_restart(self):
        gui = AutoPollEvGUI.__new__(AutoPollEvGUI)
        gui._restart_pending = True
        gui._event_queue = queue.Queue()
        gui._event_queue.put(("log", "stale log"))
        gui._event_queue.put(("new_poll", {"uid": "stale-poll"}))
        gui._event_queue.put(("cookie_expired_recapture", None))
        gui._event_queue.put(("host_restart", None))
        gui._append_log = Mock()
        gui._handle_new_poll = Mock()
        gui._start_monitoring = Mock()
        gui._set_host_controls = Mock()
        gui.start_btn = Mock()
        gui.capture_btn = Mock()
        gui.root = Mock()

        gui._process_events()

        gui._append_log.assert_not_called()
        gui._handle_new_poll.assert_not_called()
        gui._start_monitoring.assert_called_once_with()
        gui._set_host_controls.assert_called_once_with(True)
        self.assertFalse(gui._restart_pending)

    def test_restart_failure_restores_controls_and_stays_stopped(self):
        gui = AutoPollEvGUI.__new__(AutoPollEvGUI)
        gui._restart_pending = True
        gui._event_queue = queue.Queue()
        gui._event_queue.put(("host_restart", None))
        gui._start_monitoring = Mock(side_effect=RuntimeError("restart failed"))
        gui._stop_monitoring = Mock()
        gui._append_log = Mock()
        gui._set_host_controls = Mock()
        gui.start_btn = Mock()
        gui.capture_btn = Mock()
        gui.root = Mock()

        with patch("autopollev.gui.messagebox.showerror") as showerror:
            gui._process_events()

        gui._stop_monitoring.assert_called_once_with()
        gui._set_host_controls.assert_called_once_with(True)
        showerror.assert_called_once()
        self.assertFalse(gui._restart_pending)


class PollClaimResolutionTests(unittest.TestCase):
    @staticmethod
    def make_gui():
        gui = AutoPollEvGUI.__new__(AutoPollEvGUI)
        gui._monitoring = True
        gui.voter = Mock()
        gui.monitor = Mock()
        gui.monitor.answered_count = 0
        gui.config = Mock(user_choice_timeout=30)
        gui.root = Mock()
        gui._event_queue = queue.Queue()
        gui._append_log = Mock()
        gui._add_history_row = Mock()
        gui._stop_monitoring = Mock()
        gui.vote_count_label = Mock()
        return gui

    def test_locked_and_no_option_polls_are_released(self):
        cases = (
            {"title": "Locked", "state": "locked", "options": []},
            {"title": "Empty", "state": "opened", "options": []},
        )

        for poll_data in cases:
            with self.subTest(state=poll_data["state"]):
                gui = self.make_gui()
                gui.voter.get_poll_options.return_value = poll_data

                gui._handle_new_poll({"uid": "poll-1"})

                gui.monitor.mark_retryable.assert_called_once_with("poll-1")
                gui.monitor.mark_answered.assert_not_called()

    def test_only_successful_submission_completes_claim(self):
        poll_data = {
            "title": "Question",
            "state": "opened",
            "options": [{"id": 1, "value": "A"}],
        }
        dialog = Mock(selected_index=0)

        for status, answered, retryable in (
            ("success", True, False),
            ("failed", False, True),
        ):
            with self.subTest(status=status), patch(
                "autopollev.gui.PollDialog", return_value=dialog
            ), patch("autopollev.gui.notify_new_poll"):
                gui = self.make_gui()
                gui.voter.get_poll_options.return_value = poll_data
                gui.voter._submit_vote.return_value = {"status": status}

                gui._handle_new_poll({"uid": "poll-1"})

                if answered:
                    gui.monitor.mark_answered.assert_called_once_with("poll-1")
                else:
                    gui.monitor.mark_answered.assert_not_called()
                if retryable:
                    gui.monitor.mark_retryable.assert_called_once_with("poll-1")
                else:
                    gui.monitor.mark_retryable.assert_not_called()

    def test_processing_exception_releases_claim(self):
        gui = self.make_gui()
        gui.voter.get_poll_options.side_effect = RuntimeError("temporary")

        gui._handle_new_poll({"uid": "poll-1"})

        gui.monitor.mark_retryable.assert_called_once_with("poll-1")
        gui.monitor.mark_answered.assert_not_called()


if __name__ == "__main__":
    unittest.main()
