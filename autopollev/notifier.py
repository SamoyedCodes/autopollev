"""
System notifications.

Shows a system notification when a new poll is detected.
- Windows: a Toast notification via win11toast.
- macOS: a Notification Center message via osascript.
- Other platforms: silently skipped.
"""

import subprocess
import sys

from .i18n import _
from .logger import setup_logger

logger = setup_logger("autopollev.notifier")

# win11toast is only available on Windows
_win_notify = None
if sys.platform == "win32":
    try:
        from win11toast import notify as _win_notify
    except ImportError:
        logger.warning(
            "win11toast is not installed; Windows notifications are unavailable. "
            "Run: pip install win11toast"
        )


def _mac_notify(title: str, body: str):
    """Show a macOS notification via osascript."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001 - a failed notification must not break the run
        logger.debug(f"macOS notification failed: {e}")


def notify_new_poll(title: str, options: list[str] | None = None):
    """
    Send a system notification for a new poll.

    :param title: poll title
    :param options: optional list of options (used in the notification body)
    """
    body = _("notifier.title")
    if options:
        options_label = _("notifier.options_label")
        body += f"\n{options_label}: " + " / ".join(options[:5])

    full_title = f"🗳️ {title}"

    if sys.platform == "darwin":
        _mac_notify(full_title, body)
    elif sys.platform == "win32" and _win_notify is not None:
        try:
            _win_notify(
                title=full_title,
                body=body,
                app_id="AutoPollEv",
                duration="short",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Notification failed: {e}")
    # Other platforms: no-op
