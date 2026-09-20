"""Session capture picks the pollev.com cookie, not the login domain's."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub playwright so the test runs without the browser package installed.
if "playwright.sync_api" not in sys.modules:
    fake = types.ModuleType("playwright.sync_api")
    fake.Error = type("Error", (Exception,), {})
    fake.sync_playwright = None  # patched per-test
    pkg = types.ModuleType("playwright")
    pkg.sync_api = fake
    sys.modules.setdefault("playwright", pkg)
    sys.modules["playwright.sync_api"] = fake

from autopollev import session_capture

LOGGED_IN = "pollev-logged-in"


class FakePage:
    def goto(self, *args, **kwargs):
        pass


class FakeRequest:
    def __init__(self, ctx):
        self.ctx = ctx
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        # Loading a pollev.com page upgrades its session to the logged-in one.
        self.ctx.cookies_by_domain["pollev.com"] = LOGGED_IN


class FakeCtx:
    def __init__(self):
        self.cookies_by_domain = {
            "pollev.com": "pollev-anonymous",
            "id.polleverywhere.com": "id-login-session",
        }
        self.pages = [FakePage()]
        self.request = FakeRequest(self)
        self.closed = False

    def cookies(self, url=None):
        items = self.cookies_by_domain.items()
        if url:
            items = [(d, v) for d, v in items if url.endswith(d)]
        return [
            {"name": "polleverywhere_session_id", "value": v, "domain": d}
            for d, v in items
        ]

    def close(self):
        self.closed = True


def fake_playwright(ctx):
    class P:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        class chromium:
            @staticmethod
            def launch_persistent_context(*args, **kwargs):
                return ctx

    return lambda: P()


class SessionCaptureTest(unittest.TestCase):
    def test_captures_pollev_domain_cookie_after_sync(self):
        ctx = FakeCtx()
        with patch.object(
                 session_capture, "_rejection_reason",
                 lambda host, c: None
                 if c["polleverywhere_session_id"] == LOGGED_IN else "not logged in"), \
             patch("playwright.sync_api.sync_playwright", fake_playwright(ctx)), \
             patch("time.sleep", lambda s: None):
            cookies = session_capture.capture_session_id(host="somehost", timeout=10)

        self.assertEqual(cookies, {"polleverywhere_session_id": LOGGED_IN})
        self.assertIn("https://pollev.com/somehost", ctx.request.urls)
        self.assertTrue(ctx.closed)


if __name__ == "__main__":
    unittest.main()
