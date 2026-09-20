"""
Cookie authentication module.

Logs into PollEv via a cookie, with validity checking and expiry detection.
"""

import re
import requests
import time
from uuid import uuid4
from typing import Optional

from .endpoints import ENDPOINTS
from .logger import setup_logger
from .i18n import _

logger = setup_logger("autopollev.auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    """Authentication error."""


class CookieExpiredError(AuthError):
    """The cookie has expired."""


class PresenterNotFoundError(AuthError):
    """The configured presenter id does not exist — a setting to fix, not a login."""


def account_summary(identity: dict) -> str:
    """
    Turn an identity dict (from :meth:`Auth.get_account_identity`) into a single
    human-readable line for display in the CLI and GUI.

    Prefers the account email, then a name/username, then the participant id, so
    the user always sees *something* identifying to verify against — and can tell
    an anonymous/incomplete login apart from a real one.
    """
    human = identity.get("email") or identity.get("name") or identity.get("username")
    if human:
        return human
    if identity.get("participant_id"):
        return _("auth.account_participant", id=identity["participant_id"])
    return _("auth.account_unknown")


class Auth:
    """
    PollEv cookie authentication manager.

    Establishes an authenticated session from a user-provided cookie and
    provides validity checking and automatic expiry detection.
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, host: str, cookies: dict):
        """
        Initialize the authentication manager.

        :param host: PollEv presenter id (e.g. 'letingzhang727')
        :param cookies: cookie dict containing polleverywhere_session_id or pe_auth_token
        """
        self.host = host
        self._cookies = cookies
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create and configure the HTTP session."""
        session = requests.Session()
        session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'application/json',
        })

        # Inject the user-provided cookies
        for name, value in self._cookies.items():
            session.cookies.set(name, value)

        # Auto-generate visitor / visit UUID cookies
        session.cookies.set('pollev_visitor', str(uuid4()))
        session.cookies.set('pollev_visit', str(uuid4()))

        return session

    @staticmethod
    def _timestamp() -> int:
        """Return a millisecond timestamp."""
        return round(time.time() * 1000)

    def get_csrf_token(self) -> str:
        """
        Fetch a CSRF token.

        :return: the CSRF token string
        :raises AuthError: if the request fails
        """
        url = ENDPOINTS['csrf'].format(timestamp=self._timestamp())
        try:
            r = self.session.get(url, timeout=10)
            r.raise_for_status()
            return r.json()['token']
        except (requests.RequestException, KeyError) as e:
            raise AuthError(f"Failed to fetch CSRF token: {e}")

    def validate_cookie(self) -> bool:
        """
        Validate that the current cookie is valid.

        Checks login status by calling the firehose_auth endpoint.

        :return: True if the cookie is valid
        :raises CookieExpiredError: if the cookie is invalid or expired
        """
        url = ENDPOINTS['firehose_auth'].format(
            host=self.host,
            timestamp=self._timestamp()
        )
        try:
            r = self.session.get(url, timeout=10)

            if r.status_code in (401, 403):
                raise CookieExpiredError(
                    _("auth.cookie_expired", code=r.status_code)
                )

            if r.status_code == 404:
                raise PresenterNotFoundError(
                    f"Presenter '{self.host}' does not exist. "
                    f"Check the 'host' setting in config.json."
                )

            r.raise_for_status()

            # Parse the response to check validity. A non-JSON body here means
            # something answered instead of the API — a WAF bot challenge, a
            # captive portal or a proxy login page.
            try:
                data = r.json()
            except ValueError:
                raise AuthError(
                    f"registration_info returned HTTP {r.status_code} with a "
                    f"non-JSON body ({r.text[:80]!r})"
                )
            if "presenter not found" in str(data).lower():
                raise PresenterNotFoundError(f"Presenter '{self.host}' does not exist.")

            return True

        except requests.RequestException as e:
            if isinstance(e, (AuthError, CookieExpiredError)):
                raise
            raise AuthError(f"Network error while validating cookie: {e}")

    def get_firehose_token(self) -> Optional[str]:
        """
        Fetch the Firehose token.

        Some presenters do not require a token (returns null); that is normal too.

        :return: the Firehose token string, or None
        :raises CookieExpiredError: if the cookie is invalid
        """
        url = ENDPOINTS['firehose_auth'].format(
            host=self.host,
            timestamp=self._timestamp()
        )
        try:
            r = self.session.get(url, timeout=10)

            if r.status_code in (401, 403):
                raise CookieExpiredError(
                    _("auth.cookie_expired", code=r.status_code)
                )

            r.raise_for_status()
            token = r.json().get('firehose_token')
            return token

        except requests.RequestException as e:
            if isinstance(e, (AuthError, CookieExpiredError)):
                raise
            raise AuthError(f"Failed to fetch Firehose token: {e}")

    def get_account_identity(self) -> dict:
        """
        Fetch identifying info for the account tied to the current session cookie.

        Calls the profile endpoint and extracts whatever identity fields are
        available so the user can verify the *right* account was captured. Uses
        the same cookie that gets saved to config.json, so it reflects exactly
        the account AutoPollEv will vote as.

        :return: a dict with any of 'email', 'name', 'username', 'participant_id'.
            Returns {} on any failure (never raises — identity display must not
            break capture/monitoring).
        """
        url = ENDPOINTS['profile'].format(timestamp=self._timestamp())
        try:
            r = self.session.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            logger.debug("profile request failed: %s", e)
            return {}
        except ValueError:
            logger.debug(
                "profile returned HTTP %s with a non-JSON body", r.status_code
            )
            return {}

        user = data.get('user') or {}
        participant = data.get('participant') or {}
        identity: dict = {}

        email = user.get('email') or user.get('contact_email')
        if not email:
            # Some accounts expose the email under a differently-named field;
            # fall back to the first email-looking value in either object.
            for value in list(user.values()) + list(participant.values()):
                if isinstance(value, str) and _EMAIL_RE.match(value):
                    email = value
                    break
        if email:
            identity['email'] = email

        name = (
            user.get('name')
            or user.get('full_name')
            or " ".join(
                part for part in (user.get('first_name'), user.get('last_name')) if part
            ).strip()
        )
        if name:
            identity['name'] = name
        if user.get('username'):
            identity['username'] = user['username']
        if participant.get('id'):
            identity['participant_id'] = participant['id']

        return identity

    def check_response_status(self, response: requests.Response) -> bool:
        """
        Check whether an API response indicates an expired cookie.

        Call this on each API response during a run to detect expiry.

        :param response: the HTTP response object
        :return: True if the response is OK
        :raises CookieExpiredError: if the response indicates unauthorized
        """
        if response.status_code in (401, 403):
            raise CookieExpiredError(
                _("auth.cookie_expired", code=response.status_code)
            )
        return True

    def refresh_session(self, new_cookies: dict):
        """
        Refresh the session with new cookies (no program restart needed).

        :param new_cookies: the new cookie dict
        """
        self._cookies = new_cookies
        self.session.close()
        self.session = self._create_session()
        logger.info("Session refreshed with new cookies.")

    def close(self):
        """Close the HTTP session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
