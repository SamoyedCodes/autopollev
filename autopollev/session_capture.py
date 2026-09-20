"""
PollEv session capture.

Opens a Playwright-controlled Chromium window where the user logs into
pollev.com, then automatically reads the (HttpOnly) polleverywhere_session_id
cookie — no manual DevTools copy needed. Uses a persistent profile directory so
the user logs in once and later captures can run silently (used for auto-refresh).
"""

import os
import sys
import time
from typing import Callable, Optional

from .auth import Auth, AuthError, CookieExpiredError
from .config import app_dir
from .logger import setup_logger

logger = setup_logger("autopollev.session_capture")

LOGIN_URL = "https://pollev.com/login"

# Session cookie names we care about (polleverywhere_session_id preferred).
SESSION_KEYS = ("polleverywhere_session_id", "pe_auth_token")

# Persistent browser profile directory (next to config.json); gitignored.
DEFAULT_PROFILE_DIR = os.path.join(app_dir(), ".pw_profile")

# Frozen builds ship Chromium inside the bundled playwright package; "0" tells
# Playwright to look there instead of %LOCALAPPDATA%\ms-playwright.
if getattr(sys, "frozen", False):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


class SessionCaptureError(Exception):
    """Session capture failed (e.g. Playwright not installed / Chromium missing)."""


def _validates(host: str, cookies: dict) -> bool:
    """
    Check that the cookie is usable and belongs to a logged-in account.

    PollEv creates a valid participant session on the login page, so a successful
    registration_info response alone does not prove that login is complete.
    """
    try:
        auth = Auth(host=host, cookies=cookies)
        try:
            auth.validate_cookie()
            identity = auth.get_account_identity()
            return bool(
                identity.get("email")
                or identity.get("name")
                or identity.get("username")
            )
        finally:
            auth.close()
    except (CookieExpiredError, AuthError):
        return False
    except Exception:  # noqa: BLE001 - network/other errors mean "not valid yet"
        return False


def capture_session_id(
    host: str,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    login_url: str = LOGIN_URL,
    timeout: float = 300,
    on_status: Optional[Callable[[str], None]] = None,
    headless: bool = False,
) -> Optional[dict]:
    """
    Open the login window and capture a valid PollEv session cookie.

    :param host: presenter id, used to validate the cookie.
    :param profile_dir: persistent browser profile directory (stores the login).
    :param login_url: login page URL.
    :param timeout: maximum seconds to wait for the user to finish logging in.
    :param on_status: progress callback receiving one line of human-readable text.
    :param headless: if True, run without a UI (only for silent refresh when the
        session is still valid).
    :return: a dict like {"polleverywhere_session_id": "..."}, or None on failure.
    :raises SessionCaptureError: if Playwright is unavailable or Chromium is missing.
    """

    def status(msg: str):
        logger.info(msg)
        if on_status:
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001 - callback errors must not stop capture
                pass

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SessionCaptureError(
            "Playwright is not installed. Run: "
            "pip install playwright && playwright install chromium"
        ) from e

    os.makedirs(profile_dir, exist_ok=True)

    last_candidate: Optional[dict] = None

    try:
        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    profile_dir,
                    headless=headless,
                    args=["--no-default-browser-check", "--no-first-run"],
                )
            except PlaywrightError as e:
                raise SessionCaptureError(
                    "Failed to launch Chromium. First run: playwright install chromium\n"
                    f"{e}"
                ) from e

            status("Opening the login window — please log in to pollev.com…")

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightError:
                pass  # a navigation timeout/failure is not fatal; keep polling cookies

            deadline = time.time() + timeout
            waiting_logged = False

            while time.time() < deadline:
                try:
                    if not ctx.pages:  # user closed the window
                        break
                    raw = ctx.cookies()
                except PlaywrightError:
                    break  # browser was closed

                candidate = {
                    c["name"]: c["value"]
                    for c in raw
                    if c["name"] in SESSION_KEYS and c.get("value")
                }

                if candidate.get("polleverywhere_session_id"):
                    last_candidate = candidate
                    if _validates(host, candidate):
                        status("✅ Captured a valid session cookie.")
                        try:
                            ctx.close()
                        except PlaywrightError:
                            pass
                        return candidate
                    elif not waiting_logged:
                        status("Session detected — waiting for you to finish logging in…")
                        waiting_logged = True

                time.sleep(1)

            try:
                ctx.close()
            except PlaywrightError:
                pass

    except SessionCaptureError:
        raise
    except Exception as e:  # noqa: BLE001 - surface anything else as a clear error
        raise SessionCaptureError(f"Session capture failed: {e}") from e

    # Window closed before validation passed: re-check the last candidate once.
    if last_candidate and _validates(host, last_candidate):
        return last_candidate

    status("No valid session captured (login not completed or timed out).")
    return None
