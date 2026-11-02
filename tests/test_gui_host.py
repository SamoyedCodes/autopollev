"""Regression tests for GUI hostname restart event handling."""

import queue
import unittest
from unittest.mock import Mock, patch

from autopollev.gui import AutoPollEvGUI, PollDialog


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
            ("answered", True, False),
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

    def test_text_answer_is_passed_without_an_option_id(self):
        gui = self.make_gui()
        gui.voter.get_poll_options.return_value = {'title': 'Question', 'state': 'opened'}
        gui.voter._submit_vote.return_value = {'status': 'blocked'}
        with patch('autopollev.gui.PollDialog', return_value=Mock(answer='Not sure')), \
                patch('autopollev.gui.notify_new_poll'):
            gui._handle_new_poll({'uid': 'text', 'type': 'free_text_poll', 'survey_uid': 'survey'})
        gui.voter._submit_vote.assert_called_once_with(
            'text', None, 'Not sure', gui.voter.get_poll_options.return_value,
            'survey', 'free_text_poll')
        gui.monitor.mark_answered.assert_not_called()
        gui.monitor.mark_retryable.assert_called_once_with('text')

    def test_stop_or_host_change_during_text_dialog_prevents_submission(self):
        for change in ('stop', 'host'):
            with self.subTest(change=change):
                gui = self.make_gui()
                original_voter, original_monitor = gui.voter, gui.monitor
                gui.voter.get_poll_options.return_value = {'title': 'Question'}
                def change_session(_dialog):
                    if change == 'stop':
                        gui._monitoring = False
                    else:
                        gui.voter, gui.monitor = Mock(), Mock()
                gui.root.wait_window.side_effect = change_session
                with patch('autopollev.gui.PollDialog', return_value=Mock(answer='Not sure')), \
                        patch('autopollev.gui.notify_new_poll'):
                    gui._handle_new_poll({'uid': 'text', 'type': 'free_text_poll'})
                original_voter._submit_vote.assert_not_called()
                original_monitor.mark_retryable.assert_called_once_with('text')


class TextDialogTests(unittest.TestCase):
    def make_dialog(self):
        dialog = PollDialog.__new__(PollDialog)
        dialog.is_text = True
        dialog.answer = None
        dialog.selected_index = None
        dialog._cancelled = False
        dialog._timer_id = None
        dialog.timeout = 30
        dialog.answer_entry = Mock()
        dialog.bell = Mock()
        dialog.destroy = Mock()
        dialog.after = Mock(return_value='timer')
        dialog.after_cancel = Mock()
        dialog.progress = {}
        dialog.countdown_label = Mock()
        return dialog

    def test_blank_answer_keeps_dialog_open(self):
        dialog = self.make_dialog()
        dialog.answer_entry.get.return_value = '  '
        dialog._on_submit_text()
        dialog.destroy.assert_not_called()
        self.assertIsNone(dialog.answer)

    def test_submit_finishes_once_and_cancels_timer(self):
        dialog = self.make_dialog()
        dialog._timer_id = 'timer'
        dialog.answer_entry.get.return_value = ' My answer '
        dialog._on_submit_text()
        dialog._on_timeout()
        dialog._on_submit_text()
        self.assertEqual('My answer', dialog.answer)
        dialog.destroy.assert_called_once()
        dialog.after_cancel.assert_called_once_with('timer')

    def test_timeout_replaces_draft_and_finishes_once(self):
        dialog = self.make_dialog()
        dialog.answer_entry.get.return_value = 'unfinished'
        dialog._on_timeout()
        dialog._on_timeout()
        self.assertEqual('Not sure', dialog.answer)
        dialog.destroy.assert_called_once()

    def test_countdown_waits_for_full_duration(self):
        dialog = self.make_dialog()
        with patch('autopollev.gui.time.monotonic', side_effect=[100, 100, 129.9, 130]):
            dialog._start_countdown()
            self.assertEqual(30, dialog.progress['value'])
            dialog._tick()
            dialog.destroy.assert_not_called()
            dialog._tick()
        self.assertEqual('Not sure', dialog.answer)
        dialog.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
