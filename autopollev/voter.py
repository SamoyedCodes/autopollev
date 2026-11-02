"""
Auto-vote module.

Fetches poll options and submits votes. Two modes:
1. Auto mode (vote) — pick a random option and submit immediately.
2. Interactive mode (interactive_vote) — show options and wait for the user;
   auto-submit on timeout.
"""

import random
import time
import sys
import os
import codecs
import requests
from typing import Optional

from .endpoints import ENDPOINTS
from .auth import Auth, AuthError, CookieExpiredError, json_or_raise
from .logger import setup_logger, VoteHistoryLogger
from .notifier import notify_new_poll
from .i18n import _

logger = setup_logger("autopollev.voter")


class VoteError(Exception):
    """Vote error."""


def stdin_is_tty() -> bool:
    """Whether there is a terminal to read keystrokes or a prompt from."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


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

    def get_poll_options(self, poll_uid: str,
                         poll_type: str = 'multiple_choice_poll') -> dict:
        """
        Fetch a poll's option details.

        :param poll_uid: poll unique identifier
        :return: poll data dict (includes the options list)
        :raises VoteError: if the request fails
        :raises CookieExpiredError: when the cookie is expired
        """
        if poll_type == 'unknown':
            poll_type = 'multiple_choice_poll'
        if poll_type not in ('multiple_choice_poll', 'free_text_poll'):
            raise VoteError(f"Unsupported poll type: {poll_type}")
        url = ENDPOINTS['poll_data'].format(uid=poll_uid, poll_type=poll_type)
        try:
            r = self.auth.session.get(url, timeout=10)
            self.auth.check_response_status(r)
            r.raise_for_status()
            return json_or_raise(r, f"poll {poll_uid}")
        except CookieExpiredError:
            raise
        except AuthError as e:
            raise VoteError(str(e))
        except requests.RequestException as e:
            raise VoteError(f"Failed to fetch poll options: {e}")

    def _own_answer(self, poll_uid: str, poll_data: dict,
                    survey_uid: Optional[str]) -> Optional[str]:
        """This participant's current answer to the poll, or None if unanswered."""
        url = (ENDPOINTS['survey_results'].format(uid=survey_uid) if survey_uid
               else ENDPOINTS['respond_to_poll'].format(uid=poll_uid))
        try:
            r = self.auth.session.get(url, timeout=10)
            self.auth.check_response_status(r)
            r.raise_for_status()
            results = json_or_raise(r, "own results")
        except requests.RequestException as e:
            raise VoteError(f"Could not check for an existing answer: {e}")
        for result in results:
            if result.get('poll_id') == poll_data.get('id'):
                return str(result.get('value'))
        return None

    def _submit_vote(self, poll_uid: str, option_id: int | None,
                     option_value: str, poll_data: dict,
                     survey_uid: Optional[str] = None,
                     poll_type: str = 'multiple_choice_poll') -> Optional[dict]:
        """
        Submit a vote to the PollEv server.

        :param poll_uid: poll unique identifier
        :param option_id: option id
        :param option_value: option display text
        :param poll_data: the poll as get_poll_options returned it
        :param survey_uid: the activated survey this poll belongs to, if any
        :return: the response data
        """
        poll_title = poll_data.get('title', 'Untitled')
        if poll_type == 'free_text_poll':
            # Enable only after a real client request verifies the text payload.
            logger.warning(_("vote.text_unverified"))
            self.history.record_vote(
                poll_id=poll_uid, poll_title=poll_title,
                selected_option=option_value, selected_option_id=None,
                status='blocked', response_code=0,
            )
            return {'status': 'blocked', 'code': 0}
        if poll_type not in ('multiple_choice_poll', 'unknown'):
            raise VoteError(f"Unsupported poll type: {poll_type}")

        # Polls allow edited answers, so posting would replace an answer given
        # by hand on pollev.com while the dialog was open. Check right before
        # posting; the monitor's check ran when the poll was first seen.
        mine = self._own_answer(poll_uid, poll_data, survey_uid)
        if mine is not None:
            logger.info(_("vote.already_answered", choice=mine))
            self.history.record_vote(
                poll_id=poll_uid, poll_title=poll_title,
                selected_option=mine, selected_option_id=None,
                status='answered', response_code=0,
            )
            return {'status': 'answered', 'value': mine}

        # Get a CSRF token
        csrf_token = self.auth.get_csrf_token()

        if survey_uid:
            # While only the survey is activated, the poll's own results URL
            # is a 404; the web client posts through the survey instead.
            # ponytail: always the multi-page source; a one-page survey
            # (is_full_page) sends activated_one_page_survey if PollEv
            # ever starts rejecting the mismatch.
            r = self.auth.session.post(
                ENDPOINTS['survey_results'].format(uid=survey_uid),
                headers={'x-csrf-token': csrf_token},
                json=[{
                    'option_id': option_id,
                    # A survey poll's participant_key differs from its
                    # permalink; the permalink is a 404 ResourceNotFound.
                    'participant_key': poll_data.get('participant_key', poll_uid),
                    'activity_type': 'multiple_choice_poll',
                    'source': 'activated_multi_page_survey',
                }],
                timeout=10,
            )
        else:
            r = self.auth.session.post(
                ENDPOINTS['respond_to_poll'].format(uid=poll_uid),
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
                if 'no longer respond' in error_detail:
                    # Answered already, usually by hand on pollev.com.
                    # Retrying only re-asks the user and fails again.
                    logger.info("☑️ Already answered this poll; skipping it.")
                    return {"status": "answered", "code": 422}
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
        except ValueError:
            data = None
        # The survey endpoint answers with a list; callers read .get().
        if not isinstance(data, dict):
            data = {"code": r.status_code}
        data["status"] = status
        return data

    def vote(self, poll_uid: str,
             survey_uid: Optional[str] = None,
             poll_type: str = 'multiple_choice_poll') -> Optional[dict]:
        """
        Vote randomly on the given poll (auto mode, no user wait).

        :param poll_uid: poll unique identifier
        :return: the vote response data, or None (on failure)
        """
        try:
            poll_data = self.get_poll_options(poll_uid, poll_type)
            options = poll_data.get('options', [])
            poll_title = poll_data.get('title', 'Untitled')

            state = poll_data.get('state', 'opened')
            if state != 'opened':
                if getattr(self, '_last_locked_poll', None) != poll_uid:
                    logger.warning(f"⏳ Poll [{poll_title}] is currently '{state}', waiting for it to open...")
                    self._last_locked_poll = poll_uid
                return {"status": "locked", "code": 422}

            self._last_locked_poll = None

            if poll_type == 'free_text_poll':
                return self._submit_vote(poll_uid, None, 'Not sure', poll_data,
                                         survey_uid, poll_type)

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

            return self._submit_vote(poll_uid, option_id, option_value,
                                     poll_data, survey_uid)

        except CookieExpiredError:
            raise
        except (AuthError, VoteError) as e:
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

    def interactive_vote(self, poll_uid: str, timeout: float = 30,
                         should_stop=None,
                         survey_uid: Optional[str] = None,
                         poll_type: str = 'multiple_choice_poll') -> Optional[dict]:
        """
        Interactive vote: show options, wait for user input, auto-submit a
        random choice on timeout.

        :param poll_uid: poll unique identifier
        :param timeout: seconds to wait for user input
        :param should_stop: called while waiting; truthy means the user asked
            to quit, so no vote is submitted
        :return: the vote response data, or None
        """
        try:
            poll_data = self.get_poll_options(poll_uid, poll_type)
            options = poll_data.get('options', [])
            poll_title = poll_data.get('title', 'Untitled')

            state = poll_data.get('state', 'opened')
            if state != 'opened':
                if getattr(self, '_last_locked_poll', None) != poll_uid:
                    logger.warning(f"⏳ Poll [{poll_title}] is currently '{state}', waiting for it to open...")
                    self._last_locked_poll = poll_uid
                return {"status": "locked", "code": 422}

            self._last_locked_poll = None

            is_text = poll_type == 'free_text_poll'
            if not options and not is_text:
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
            if is_text:
                print(_("dialog.text_unavailable"))
                print(_("vote.text_prompt", timeout=timeout))
            else:
                print(f"\n  ⏳ Enter an option number within {int(timeout)}s (a random choice is made on timeout)")
            print(f"  \033[90m{'─'*50}\033[0m")

            # Wait for user input (with timeout)
            user_choice = self._input_with_timeout(
                timeout, None if is_text else len(options), should_stop
            )

            if should_stop is not None and should_stop():
                # Ctrl-C during the countdown used to be ignored: the prompt
                # ran to timeout and voted anyway on the way out.
                logger.info(_("vote.aborted"))
                return None

            if is_text:
                answer = user_choice if user_choice is not None else 'Not sure'
                return self._submit_vote(poll_uid, None, answer, poll_data,
                                         survey_uid, poll_type)

            if user_choice is not None:
                selected = options[user_choice]
                logger.info(f"👤 You selected: {option_values[user_choice]}")
            else:
                selected = random.choice(options)
                logger.info(f"⏰ Timeout! Random choice: {selected.get('value', selected.get('keyword', '?'))}")

            option_id = selected.get('id')
            option_value = selected.get('value', selected.get('keyword', 'Unknown option'))

            return self._submit_vote(poll_uid, option_id, option_value,
                                     poll_data, survey_uid)

        except CookieExpiredError:
            raise
        except (AuthError, VoteError) as e:
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

    def _input_with_timeout(self, timeout: float, max_option: int | None,
                            should_stop=None) -> int | str | None:
        """
        User input with a timeout and countdown. Avoids the built-in input()
        so it does not block background threads.

        :param timeout: timeout in seconds
        :param max_option: the highest valid option number, or None for text
        :param should_stop: called each tick; truthy aborts the wait
        :return: option index, submitted text, or None on timeout/cancellation
        """
        if not stdin_is_tty():
            # Piped stdin, a service, an IDE console: putting the terminal in
            # cbreak mode raises there, which used to abandon the vote
            # entirely. Wait the countdown out and let the caller pick.
            logger.info(_("vote.no_terminal", timeout=timeout))
            end_time = time.monotonic() + timeout
            while time.monotonic() < end_time:
                if should_stop is not None and should_stop():
                    return None
                time.sleep(0.1)
            return None

        start_time = time.monotonic()
        result_str = ""
        last_remaining = int(timeout) + 1

        if sys.platform != 'win32':
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            decoder = codecs.getincrementaldecoder(sys.stdin.encoding or 'utf-8')()

        try:
            while time.monotonic() - start_time < timeout:
                if should_stop is not None and should_stop():
                    return None
                remaining = int(timeout - (time.monotonic() - start_time))
                if remaining != last_remaining:
                    # \033[K clears to end of line
                    prompt = 'Enter answer' if max_option is None else 'Enter option number'
                    sys.stdout.write(f"\r  ⏳ {remaining}s left | >>> {prompt}: {result_str}\033[K")
                    sys.stdout.flush()
                    last_remaining = remaining

                char = None
                if sys.platform == 'win32':
                    import msvcrt
                    if msvcrt.kbhit():
                        char = msvcrt.getwch()
                        if char in ('\x00', '\xe0'):
                            msvcrt.getwch()  # Discard an extended key's scan code.
                            char = None
                else:
                    import select
                    if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
                        # TextIO read-ahead hides pasted characters from select().
                        char = decoder.decode(os.read(fd, 1))

                if char:
                    if char in ('\r', '\n'):
                        print()
                        if max_option is None:
                            if result_str.strip():
                                return result_str.strip()
                            print(_("vote.text_blank"))
                            result_str = ""
                            last_remaining = -1
                            continue
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

        print("\r  ⏰ Time's up! Using the fallback answer...\033[K")
        return None
