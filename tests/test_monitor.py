"""Regression tests for Firehose poll detection."""

import json
import unittest
from unittest.mock import patch

import requests

from autopollev.monitor import PollMonitor


class FakeResponse:
    def __init__(self, payload, status_code=200, text="<html>challenge</html>"):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)

    def get(self, _url, timeout):
        if not self.responses:
            raise AssertionError("No fake response remains")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAuth:
    host = "presenter"

    def __init__(self, *responses):
        self.session = FakeSession(*responses)

    @staticmethod
    def check_response_status(_response):
        return True

    @staticmethod
    def get_firehose_token():
        return None


class PollMonitorDetectionTests(unittest.TestCase):
    def make_monitor(self, *payloads):
        responses = [FakeResponse(payload) for payload in payloads]
        return PollMonitor(FakeAuth(*responses))

    def test_detects_legacy_json_string_message(self):
        payload = {
            "message": json.dumps(
                {"uid": "poll-legacy", "type": "multiple_choice_poll"}
            )
        }

        poll = self.make_monitor(payload).check_new_poll()

        self.assertEqual("poll-legacy", poll["uid"])
        self.assertEqual("multiple_choice_poll", poll["type"])

    def test_detects_message_that_is_already_an_object(self):
        payload = {
            "message": {
                "uid": "poll-object",
                "type": "multiple_choice_poll",
            }
        }

        poll = self.make_monitor(payload).check_new_poll()

        self.assertEqual("poll-object", poll["uid"])

    def test_detects_nested_poll_in_message_list(self):
        payload = {
            "data": {
                "messages": [
                    {"event": "heartbeat"},
                    {
                        "activity_type": "multiple_choice_poll",
                        "activity": {"poll": {"permalink": "poll-nested"}},
                    },
                ]
            }
        }

        poll = self.make_monitor(payload).check_new_poll()

        self.assertEqual("poll-nested", poll["uid"])
        self.assertEqual("multiple_choice_poll", poll["type"])

    def test_detects_double_encoded_message(self):
        encoded = json.dumps(json.dumps({"poll_uid": "poll-double"}))

        poll = self.make_monitor({"message": encoded}).check_new_poll()

        self.assertEqual("poll-double", poll["uid"])
        self.assertEqual("unknown", poll["type"])

    def test_suppresses_an_in_flight_poll_after_cooldown(self):
        payload = {"message": {"uid": "poll-repeat"}}
        monitor = self.make_monitor(payload, payload)

        with patch(
            "autopollev.monitor.time.time",
            side_effect=[99.0, 100.0, 119.0, 120.0],
        ):
            first = monitor.check_new_poll()
            second = monitor.check_new_poll()

        self.assertEqual("poll-repeat", first["uid"])
        self.assertIsNone(second)

    def test_payload_changes_do_not_bypass_in_flight_claim(self):
        first_payload = {"message": {"uid": "poll-changing", "sequence": 1}}
        changed_payload = {"message": {"uid": "poll-changing", "sequence": 2}}
        monitor = self.make_monitor(first_payload, changed_payload)

        with patch(
            "autopollev.monitor.time.time",
            side_effect=[99.0, 100.0, 119.0, 120.0],
        ):
            first = monitor.check_new_poll()
            duplicate = monitor.check_new_poll()

        self.assertEqual("poll-changing", first["uid"])
        self.assertIsNone(duplicate)

    def test_retryable_poll_observes_uid_cooldown(self):
        first_payload = {"message": {"uid": "poll-retry", "sequence": 1}}
        changed_payload = {"message": {"uid": "poll-retry", "sequence": 2}}
        monitor = self.make_monitor(
            first_payload, changed_payload, changed_payload
        )

        with patch.object(PollMonitor, "_timestamp", return_value=0), patch(
            "autopollev.monitor.time.time", side_effect=[100.0, 105.0, 116.0]
        ):
            first = monitor.check_new_poll()
            monitor.mark_retryable(first["uid"])
            during_cooldown = monitor.check_new_poll()
            after_cooldown = monitor.check_new_poll()

        self.assertIsNone(during_cooldown)
        self.assertEqual("poll-retry", after_cooldown["uid"])

    def test_empty_message_is_normal_no_active_poll_response(self):
        monitor = self.make_monitor({"message": None})

        with self.assertNoLogs("autopollev.monitor", level="WARNING"):
            poll = monitor.check_new_poll()

        self.assertIsNone(poll)

    def test_unrecognized_payload_is_logged_once(self):
        payload = {"message": {"event": "heartbeat"}}
        monitor = self.make_monitor(payload, payload)

        with self.assertLogs("autopollev.monitor", level="WARNING") as logs:
            self.assertIsNone(monitor.check_new_poll())
            self.assertIsNone(monitor.check_new_poll())

        matching = [
            line for line in logs.output if "unrecognized payload" in line
        ]
        self.assertEqual(1, len(matching))

    def test_http_error_is_not_misreported_as_a_poll(self):
        auth = FakeAuth(FakeResponse({"message": {"uid": "bad"}}, 500))
        monitor = PollMonitor(auth)

        poll = monitor.check_new_poll()

        self.assertIsNone(poll)
        self.assertEqual(1, monitor._retry_count)

    def test_same_uid_can_be_detected_after_no_active_poll(self):
        active = {"message": {"uid": "poll-reactivated"}}
        monitor = self.make_monitor(active, {"message": None}, active)

        first = monitor.check_new_poll()
        monitor.mark_answered(first["uid"])
        inactive = monitor.check_new_poll()
        reactivated = monitor.check_new_poll()

        self.assertIsNone(inactive)
        self.assertEqual("poll-reactivated", reactivated["uid"])
        self.assertEqual(1, monitor.answered_count)

        monitor.mark_answered(reactivated["uid"])
        self.assertEqual(2, monitor.answered_count)

    def test_answered_current_activation_is_not_detected_twice(self):
        active = {"message": {"uid": "poll-current"}}
        monitor = self.make_monitor(active, active)

        with patch(
            "autopollev.monitor.time.time",
            side_effect=[99.0, 100.0, 119.0, 120.0],
        ):
            first = monitor.check_new_poll()
            monitor.mark_answered(first["uid"])
            duplicate = monitor.check_new_poll()

        self.assertIsNone(duplicate)
        self.assertEqual(1, monitor.answered_count)

    def test_read_timeout_preserves_answered_activation(self):
        active = {"message": {"uid": "poll-timeout"}}
        monitor = PollMonitor(
            FakeAuth(
                FakeResponse(active),
                requests.exceptions.ReadTimeout(),
                FakeResponse(active),
            )
        )

        with patch.object(PollMonitor, "_timestamp", return_value=0), patch(
            "autopollev.monitor.time.time", side_effect=[100.0, 120.0]
        ):
            first = monitor.check_new_poll()
            monitor.mark_answered(first["uid"])
            timeout = monitor.check_new_poll()
            duplicate = monitor.check_new_poll()

        self.assertIsNone(timeout)
        self.assertIsNone(duplicate)
        self.assertEqual(1, monitor.answered_count)

    def test_answered_count_is_idempotent(self):
        monitor = self.make_monitor({"message": {"uid": "poll-count"}})

        poll = monitor.check_new_poll()
        monitor.mark_answered(poll["uid"])
        monitor.mark_answered(poll["uid"])

        self.assertEqual(1, monitor.answered_count)


class NonJsonResponseTests(unittest.TestCase):
    """A WAF challenge or proxy page must be named, not counted as a hiccup."""

    def test_html_body_is_reported_once_and_does_not_raise(self):
        monitor = PollMonitor(FakeAuth(
            FakeResponse(ValueError("no json")),
            FakeResponse(ValueError("no json")),
        ))

        with patch("autopollev.monitor.logger") as log:
            self.assertIsNone(monitor.check_new_poll())
            self.assertIsNone(monitor.check_new_poll())

        self.assertEqual(log.warning.call_count, 1)


if __name__ == "__main__":
    unittest.main()
