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
from .endpoints import ENDPOINTS
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


def _page_url(ctx) -> str:
    """Current URL of the login window, for diagnostics."""
    try:
        return ctx.pages[0].url if ctx.pages else "(no window)"
    except Exception:  # noqa: BLE001 - diagnostics must never break capture
        return "(unknown)"


def _cookie_domains(ctx) -> str:
    """Which domains hold a session cookie — names only, never values."""
    try:
        return ", ".join(sorted({
            f"{c['domain']}:{c['name']}"
            for c in ctx.cookies() if c["name"] in SESSION_KEYS
        })) or "(none)"
    except Exception:  # noqa: BLE001 - diagnostics must never break capture
        return "(unknown)"


def _rejection_reason(host: str, cookies: dict) -> Optional[str]:
    """
    Return None when the cookie is a logged-in session, else why it is not.

    PollEv creates a valid participant session on the login page, so a successful
    registration_info response alone does not prove that login is complete. The
    reason is what makes a failed capture diagnosable from a log alone.
    """
    try:
        auth = Auth(host=host, cookies=cookies)
        try:
            auth.validate_cookie()
            identity = auth.get_account_identity()
            if identity.get("email") or identity.get("name") or identity.get("username"):
                return None
            return "profile has no account yet (still an anonymous participant session)"
        finally:
            auth.close()
    except (CookieExpiredError, AuthError) as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 - network/other errors mean "not valid yet"
        return f"{type(e).__name__}: {e}"



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
            ticks = 0

            while time.time() < deadline:
                try:
                    if not ctx.pages:  # user closed the window
                        break
                    # Scope to pollev.com: the login flow also sets cookies named
                    # polleverywhere_session_id on id./www.polleverywhere.com, and an
                    # unscoped read picks whichever one comes back last.
                    raw = ctx.cookies("https://pollev.com")
                except PlaywrightError:
                    break  # browser was closed

                candidate = {
                    c["name"]: c["value"]
                    for c in raw
                    if c["name"] in SESSION_KEYS and c.get("value")
                }

                reason = "no pollev.com session cookie yet"
                if candidate.get("polleverywhere_session_id"):
                    last_candidate = candidate
                    reason = _rejection_reason(host, candidate)
                    if reason is None:
                        status("✅ Captured a valid session cookie.")
                        try:
                            ctx.close()
                        except PlaywrightError:
                            pass
                        return candidate
                    if not waiting_logged:
                        status("Session detected — waiting for you to finish logging in…")
                        waiting_logged = True

                # Every 15s, say what is actually blocking. Without this a stuck
                # capture looks identical whether the user never logged in, the
                # login landed on another domain, or the API rejected the cookie.
                if ticks and ticks % 15 == 0:
                    status(f"Still waiting — {reason}")
                    status(f"   window is on: {_page_url(ctx)}")
                    status(f"   session cookies: {_cookie_domains(ctx)}")

                # pollev.com/login hands off to id.polleverywhere.com, which has its
                # own session. The pollev.com session only becomes the logged-in one
                # once a pollev.com page is loaded again, so poke one periodically.
                if ticks % 3 == 0:
                    try:
                        ctx.request.get(
                            ENDPOINTS["home"].format(host=host), timeout=10000
                        )
                    except Exception:  # noqa: BLE001 - best-effort sync
                        pass

                ticks += 1
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
    if last_candidate and _rejection_reason(host, last_candidate) is None:
        return last_candidate

    status(
        "No valid session captured (login not completed or timed out). "
        "Finish logging in inside the window, then open pollev.com once."
    )
    return None
