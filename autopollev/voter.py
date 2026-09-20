"""
Auto-vote module.

Fetches poll options and submits votes. Two modes:
1. Auto mode (vote) — pick a random option and submit immediately.
2. Interactive mode (interactive_vote) — show options and wait for the user;
   auto-submit on timeout.
"""

import random
import time
import threading
import sys
import requests
from typing import Optional

from .endpoints import ENDPOINTS
from .auth import Auth, CookieExpiredError
from .logger import setup_logger, VoteHistoryLogger
from .notifier import notify_new_poll
from .i18n import _

logger = setup_logger("autopollev.voter")


class VoteError(Exception):
    """Vote error."""


class Voter:
    """
    Auto-voter.

    Fetches a poll's option list and supports interactive and automatic voting.
    """

    def __init__(self, auth: Auth, history_logger: VoteHistoryLogger):
        """
        Initialize the voter.

        :param auth: an authenticated Auth instance
        :param history_logger: the vote history logger
        """
        self.auth = auth
        self.history = history_logger
        self._last_locked_poll = None

    def get_poll_options(self, poll_uid: str) -> dict:
        """
        Fetch a poll's option details.

        :param poll_uid: poll unique identifier
        :return: poll data dict (includes the options list)
        :raises VoteError: if the request fails
        :raises CookieExpiredError: when the cookie is expired
        """
        url = ENDPOINTS['poll_data'].format(uid=poll_uid)
        try:
            r = self.auth.session.get(url, timeout=10)
            self.auth.check_response_status(r)
            r.raise_for_status()
            return r.json()
        except CookieExpiredError:
            raise
        except requests.RequestException as e:
            raise VoteError(f"Failed to fetch poll options: {e}")

    def _submit_vote(self, poll_uid: str, option_id: int,
                     option_value: str, poll_title: str) -> Optional[dict]:
        """
        Submit a vote to the PollEv server.

        :param poll_uid: poll unique identifier
        :param option_id: option id
        :param option_value: option display text
        :param poll_title: poll title
        :return: the response data
        """
        # Get a CSRF token
        csrf_token = self.auth.get_csrf_token()

        # Submit the vote
        url = ENDPOINTS['respond_to_poll'].format(uid=poll_uid)
        r = self.auth.session.post(
            url,
            headers={'x-csrf-token': csrf_token},
            data={
                'option_id': option_id,
                'isPending': True,
                'source': 'pollev_page',
            },
            timeout=10,
        )

        self.auth.check_response_status(r)

        # Check whether the poll is locked (HTTP 422)
        if r.status_code == 422:
            try:
                error_data = r.json()
                error_detail = str(error_data).lower()
                if 'locked' in error_detail:
                    logger.warning("🔒 Poll is locked; the presenter hasn't opened voting yet.")
                    return {"status": "locked", "code": 422}
            except (ValueError, KeyError):
                pass

        # Record the vote result
        status = "success" if r.status_code in (200, 201) else "failed"

        if status == "success":
            logger.info(_("vote.success", choice=option_value))
        else:
            logger.warning(_("vote.fail", code=r.status_code, text=r.text[:200]))

        self.history.record_vote(
            poll_id=poll_uid,
            poll_title=poll_title,
            selected_option=str(option_value),
            selected_option_id=option_id,
            status=status,
            response_code=r.status_code,
        )

        try:
            data = r.json()
            if isinstance(data, dict):
                data["status"] = status
            return data
        except ValueError:
            return {"status": status, "code": r.status_code}

    def vote(self, poll_uid: str) -> Optional[dict]:
        """
        Vote randomly on the given poll (auto mode, no user wait).

        :param poll_uid: poll unique identifier
        :return: the vote response data, or None (on failure)
        """
        try:
            poll_data = self.get_poll_options(poll_uid)
            options = poll_data.get('options', [])
            poll_title = poll_data.get('title', 'Untitled')

            state = poll_data.get('state', 'opened')
            if state != 'opened':
                if getattr(self, '_last_locked_poll', None) != poll_uid:
                    logger.warning(f"⏳ Poll [{poll_title}] is currently '{state}', waiting for it to open...")
                    self._last_locked_poll = poll_uid
                return {"status": "locked", "code": 422}

            self._last_locked_poll = None

            if not options:
                # Do not record vote history for polls without options (likely closed or unsupported)
                logger.warning(f"Poll [{poll_uid}] has no options (state: {state}), skipping.")
                return None

            selected = random.choice(options)
            option_id = selected.get('id')
            option_value = selected.get('value', selected.get('keyword', 'Unknown option'))

            logger.info(_("vote.poll_title", title=poll_title))
            logger.info(_("vote.options", options=[o.get('value', o.get('keyword', '?')) for o in options]))
            logger.info(_("vote.random_choice", choice=option_value))

            return self._submit_vote(poll_uid, option_id, option_value, poll_title)

        except CookieExpiredError:
            raise
        except VoteError as e:
            logger.error(_("vote.error", error=e))
            self.history.record_vote(
                poll_id=poll_uid, poll_title="fetch_failed",
                selected_option="N/A", selected_option_id=0,
                status="error", response_code=0,
            )
            return None
        except Exception as e:
            logger.error(_("vote.unknown_error", error=e))
            return None

    def interactive_vote(self, poll_uid: str, timeout: float = 30) -> Optional[dict]:
        """
        Interactive vote: show options, wait for user input, auto-submit a
        random choice on timeout.

        :param poll_uid: poll unique identifier
        :param timeout: seconds to wait for user input
        :return: the vote response data, or None
        """
        try:
            poll_data = self.get_poll_options(poll_uid)
            options = poll_data.get('options', [])
            poll_title = poll_data.get('title', 'Untitled')

            state = poll_data.get('state', 'opened')
            if state != 'opened':
                if getattr(self, '_last_locked_poll', None) != poll_uid:
                    logger.warning(f"⏳ Poll [{poll_title}] is currently '{state}', waiting for it to open...")
                    self._last_locked_poll = poll_uid
                return {"status": "locked", "code": 422}

            self._last_locked_poll = None

            if not options:
                # Do not record vote history for polls without options (likely closed or unsupported)
                logger.warning(f"Poll [{poll_uid}] has no options (state: {state}), skipping.")
                return None

            option_values = [
                o.get('value', o.get('keyword', 'Unknown option')) for o in options
            ]

            # Send a system notification
            notify_new_poll(poll_title, option_values)

            # Show options in the terminal
            print(f"\n\033[96m{'='*50}")
            print(f"  🆕 New poll: {poll_title}")
            print(f"{'='*50}\033[0m")
            for i, val in enumerate(option_values, 1):
                print(f"  \033[93m[{i}]\033[0m {val}")
            print(f"\n  ⏳ Enter an option number within {int(timeout)}s (a random choice is made on timeout)")
            print(f"  \033[90m{'─'*50}\033[0m")

            # Wait for user input (with timeout)
            user_choice = self._input_with_timeout(timeout, len(options))

            if user_choice is not None:
                selected = options[user_choice]
                logger.info(f"👤 You selected: {option_values[user_choice]}")
            else:
                selected = random.choice(options)
                logger.info(f"⏰ Timeout! Random choice: {selected.get('value', selected.get('keyword', '?'))}")

            option_id = selected.get('id')
            option_value = selected.get('value', selected.get('keyword', 'Unknown option'))

            return self._submit_vote(poll_uid, option_id, option_value, poll_title)

        except CookieExpiredError:
            raise
        except VoteError as e:
            logger.error(f"❌ Vote failed: {e}")
            self.history.record_vote(
                poll_id=poll_uid, poll_title="fetch_failed",
                selected_option="N/A", selected_option_id=0,
                status="error", response_code=0,
            )
            return None
        except Exception as e:
            logger.error(f"❌ Unknown error: {e}")
            return None

    def _input_with_timeout(self, timeout: float, max_option: int) -> Optional[int]:
        """
        User input with a timeout and countdown. Avoids the built-in input()
        so it does not block background threads.

        :param timeout: timeout in seconds
        :param max_option: the highest valid option number
        :return: the selected option index (0-based), or None (on timeout)
        """
        import sys

        start_time = time.time()
        result_str = ""
        last_remaining = int(timeout) + 1

        if sys.platform != 'win32':
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)

        try:
            while time.time() - start_time < timeout:
                remaining = int(timeout - (time.time() - start_time))
                if remaining != last_remaining:
                    # \033[K clears to end of line
                    sys.stdout.write(f"\r  ⏳ {remaining}s left | >>> Enter option number: {result_str}\033[K")
                    sys.stdout.flush()
                    last_remaining = remaining

                char = None
                if sys.platform == 'win32':
                    import msvcrt
                    if msvcrt.kbhit():
                        try:
                            char = msvcrt.getch().decode('utf-8')
                        except UnicodeDecodeError:
                            pass
                else:
                    import select
                    if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
                        char = sys.stdin.read(1)

                if char:
                    if char in ('\r', '\n'):
                        print()
                        try:
                            choice = int(result_str.strip())
                            if 1 <= choice <= max_option:
                                return choice - 1
                            else:
                                print(f"  ⚠️ Please enter a number between 1 and {max_option}")
                                result_str = ""
                                last_remaining = -1
                        except ValueError:
                            print("\n  ⚠️ Please enter a number")
                            result_str = ""
                            last_remaining = -1
                    elif char in ('\x08', '\x7f'):  # backspace
                        if result_str:
                            result_str = result_str[:-1]
                            last_remaining = -1
                    else:
                        if char.isprintable():
                            result_str += char
                            last_remaining = -1

                time.sleep(0.05)
        finally:
            if sys.platform != 'win32':
                import termios
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        print(f"\r  ⏰ Time's up! Auto-selecting...\033[K")
        return None
