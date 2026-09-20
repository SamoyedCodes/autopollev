"""Tests for logged-in account identity extraction and display."""

import unittest
from unittest.mock import patch

import requests

from autopollev.auth import Auth, account_summary
from autopollev.session_capture import _rejection_reason


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def make_auth(response):
    """Build an Auth whose session.get returns the given fake response."""
    auth = Auth(host="presenter", cookies={"polleverywhere_session_id": "x"})
    auth.session.get = lambda *a, **k: response
    return auth


class GetAccountIdentityTests(unittest.TestCase):
    def test_extracts_email_from_user(self):
        auth = make_auth(FakeResponse({
            "participant": {"id": 42},
            "user": {"email": "jane@example.edu", "first_name": "Jane"},
        }))
        self.assertEqual(auth.get_account_identity().get("email"), "jane@example.edu")

    def test_falls_back_to_email_looking_value(self):
        # No 'email' key, but an email-shaped value elsewhere in the object.
        auth = make_auth(FakeResponse({
            "participant": {},
            "user": {"login": "jane@example.edu"},
        }))
        self.assertEqual(auth.get_account_identity().get("email"), "jane@example.edu")

    def test_builds_name_from_first_and_last(self):
        auth = make_auth(FakeResponse({
            "participant": {},
            "user": {"first_name": "Jane", "last_name": "Doe"},
        }))
        self.assertEqual(auth.get_account_identity().get("name"), "Jane Doe")

    def test_participant_only_session_has_no_email(self):
        auth = make_auth(FakeResponse({
            "participant": {"id": 12345678, "phone_number": None},
            "user": {},
        }))
        identity = auth.get_account_identity()
        self.assertNotIn("email", identity)
        self.assertEqual(identity.get("participant_id"), 12345678)

    def test_network_error_returns_empty_dict(self):
        auth = make_auth(FakeResponse("boom", status_code=500))
        self.assertEqual(auth.get_account_identity(), {})

    def test_invalid_json_returns_empty_dict(self):
        auth = make_auth(FakeResponse(ValueError("not json")))
        self.assertEqual(auth.get_account_identity(), {})


class AccountSummaryTests(unittest.TestCase):
    def test_prefers_email(self):
        self.assertEqual(
            account_summary({"email": "a@example.edu", "name": "A B", "participant_id": 1}),
            "a@example.edu",
        )

    def test_name_then_username(self):
        self.assertEqual(account_summary({"name": "Jane Doe"}), "Jane Doe")
        self.assertEqual(account_summary({"username": "jdoe"}), "jdoe")

    def test_participant_id_fallback(self):
        self.assertIn("12345678", account_summary({"participant_id": 12345678}))

    def test_empty_is_unknown(self):
        self.assertIn("unknown", account_summary({}).lower())


class SessionValidationTests(unittest.TestCase):
    @patch("autopollev.session_capture.Auth")
    def test_rejects_anonymous_participant_session(self, auth_class):
        auth_class.return_value.validate_cookie.return_value = True
        auth_class.return_value.get_account_identity.return_value = {
            "participant_id": 12345678,
        }

        reason = _rejection_reason("presenter", {"polleverywhere_session_id": "x"})
        self.assertIn("anonymous", reason)

    @patch("autopollev.session_capture.Auth")
    def test_accepts_logged_in_account_session(self, auth_class):
        auth_class.return_value.validate_cookie.return_value = True
        auth_class.return_value.get_account_identity.return_value = {
            "email": "jane@example.edu",
            "participant_id": 12345678,
        }

        self.assertIsNone(
            _rejection_reason("presenter", {"polleverywhere_session_id": "x"})
        )


if __name__ == "__main__":
    unittest.main()
