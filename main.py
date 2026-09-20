"""
AutoPollEv — PollEverywhere Auto-Vote Tool

Usage:
    python main.py              # GUI mode (default)
    python main.py --cli        # Interactive CLI (notify + wait for user choice)
    python main.py --auto       # Auto mode (silent random vote)
    python main.py --gui        # GUI mode
    python main.py --help       # Help

On first run, config.json is auto-generated — fill in your cookie and rerun.
"""

import argparse
import sys
import time
import signal

from autopollev.config import Config, ConfigError
from autopollev.auth import Auth, AuthError, CookieExpiredError, account_summary
from autopollev.monitor import PollMonitor
from autopollev.voter import Voter
from autopollev.logger import setup_logger, VoteHistoryLogger
from autopollev.i18n import _


logger = setup_logger("autopollev")

# Graceful shutdown flag
_shutdown = False


def signal_handler(signum, frame):
    global _shutdown
    _shutdown = True
    print()
    logger.info(_("main.shutdown"))


def print_banner():
    subtitle = _("banner.subtitle")
    # Fixed-width banner: pad subtitle to 36 chars
    padded = subtitle.ljust(36)
    banner = f"""
\033[96m╔══════════════════════════════════════════╗
║         🗳️  AutoPollEv v2.0.0  🗳️        ║
║   {padded}║
╚══════════════════════════════════════════╝\033[0m
"""
    print(banner)


def print_history(history_logger: VoteHistoryLogger):
    stats = history_logger.get_stats()
    history = history_logger.get_history(limit=10)

    print(f"\n\033[96m{'='*45}")
    print(f"  {_('history.title')}")
    print(f"{'='*45}\033[0m")
    print(_("history.total", total=stats['total']))
    print(f"  \033[92m{_('history.success', n=stats['success'])}\033[0m")
    print(f"  \033[91m{_('history.failed', n=stats['failed'])}\033[0m")

    if history:
        print(f"\n\033[96m  {_('history.recent', n=len(history))}\033[0m")
        for record in history:
            ts = record['timestamp'][:19]
            status_icon = "✅" if record['status'] == 'success' else "❌"
            print(f"  {status_icon} [{ts}] {record['poll_title'][:25]} → {record['selected_option']}")
    print()


def log_account(auth: Auth):
    """Log which PollEv account the current session cookie belongs to."""
    logger.info(_("login.account_checking"))
    identity = auth.get_account_identity()
    logger.info(_("login.account", account=account_summary(identity)))


def run_cli(config: Config, auto_mode: bool = False):
    global _shutdown

    signal.signal(signal.SIGINT, signal_handler)

    history_logger = VoteHistoryLogger(config.log_dir)

    logger.info(_("main.target_host", host=config.host))
    mode_key = "main.mode.auto" if auto_mode else "main.mode.interactive"
    logger.info(_("main.mode", mode=_(mode_key)))
    logger.info(_("main.validating_cookie"))

    auth = Auth(host=config.host, cookies=config.cookies)

    try:
        auth.validate_cookie()
        logger.info(_("main.cookie_ok"))
        log_account(auth)
    except CookieExpiredError as e:
        logger.error(f"❌ {e}")
        logger.error(_("main.cookie_expired_update"))
        return 1
    except AuthError as e:
        logger.warning(_("main.cookie_warn", error=e))
        logger.info(_("main.continue_anyway"))

    monitor = PollMonitor(auth, poll_interval=config.poll_interval)
    voter = Voter(auth, history_logger)

    monitor.start()

    try:
        while not _shutdown and monitor.is_running:
            try:
                poll_info = monitor.check_new_poll()

                if poll_info:
                    poll_uid = poll_info.get('uid', '')
                    poll_type = poll_info.get('type', 'unknown')
                    logger.info(_("main.new_poll", type=poll_type, uid=poll_uid))

                    if config.answer_delay > 0:
                        logger.info(_("main.answer_delay", delay=config.answer_delay))
                        time.sleep(config.answer_delay)

                    if _shutdown:
                        break

                    if auto_mode:
                        result = voter.vote(poll_uid)
                    else:
                        result = voter.interactive_vote(
                            poll_uid,
                            timeout=config.user_choice_timeout
                        )

                    if result and result.get("status") == "success":
                        monitor.mark_answered(poll_uid)
                        logger.info(_("main.voted_count", count=monitor.answered_count))
                    else:
                        # Locked and temporary failures remain eligible after
                        # the monitor's per-UID retry cooldown.
                        monitor.mark_retryable(poll_uid)

                for _tick in range(int(config.poll_interval * 10)):
                    if _shutdown:
                        break
                    time.sleep(0.1)

            except CookieExpiredError as e:
                logger.error(f"❌ {e}")
                logger.info(_("main.cookie_expired_input"))

                try:
                    input()
                    config = Config()
                    auth.refresh_session(config.cookies)
                    auth.validate_cookie()
                    logger.info(_("main.cookie_refreshed"))
                except (ConfigError, AuthError) as e:
                    logger.error(f"❌ {e}")
                    continue
                except EOFError:
                    break

    finally:
        monitor.stop()
        auth.close()

    print_history(history_logger)
    logger.info(_("main.exit"))
    return 0


def run_gui(config: Config):
    try:
        from autopollev.gui import AutoPollEvGUI
        app = AutoPollEvGUI(config)
        app.run()
        return 0
    except ImportError as e:
        logger.error(_("main.gui_error", error=e))
        logger.error(_("main.gui_tkinter"))
        return 1


def run_login(config: Config):
    """Open the login window, capture the session cookie into config.json, then exit."""
    from autopollev.session_capture import capture_session_id, SessionCaptureError

    logger.info(_("login.opening"))
    try:
        cookies = capture_session_id(
            on_status=lambda m: logger.info(m),
            headless=False,
        )
    except SessionCaptureError as e:
        logger.error(str(e))
        return 1

    if not cookies:
        logger.error(_("login.failed"))
        return 1

    for key, value in cookies.items():
        config.update_cookie(key, value)
    logger.info(_("login.saved", path=config.config_path))

    # Show whose account this session belongs to so the user can confirm the
    # right PollEv account was captured.
    auth = Auth(host=config.host, cookies=config.cookies)
    try:
        log_account(auth)
    finally:
        auth.close()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=_("argparse.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_("argparse.epilog"),
    )
    parser.add_argument('--history', action='store_true', help=_("argparse.history"))
    parser.add_argument('--cli', action='store_true', help=_("argparse.cli"))
    parser.add_argument('--auto', action='store_true', help=_("argparse.auto"))
    parser.add_argument('--gui', action='store_true', help=_("argparse.gui"))
    parser.add_argument('--login', action='store_true', help=_("argparse.login"))
    parser.add_argument('--config', type=str, default=None, help=_("argparse.config"))

    args = parser.parse_args()

    # GUI is the default; the CLI runs only when explicitly requested via --cli/--auto.
    cli_mode = args.cli or args.auto
    gui_mode = args.gui or not (cli_mode or args.login or args.history)

    # The windowed Windows build has no console, so every console-only mode would
    # run invisibly. Fall back to the GUI, which covers the same ground.
    if sys.stdout is None:
        args.history = args.login = False
        cli_mode, gui_mode = False, True

    print_banner()

    try:
        config = Config(args.config)
    except ConfigError as e:
        # For GUI / --login, the first run just created the template; retry so
        # the user can capture their session right away instead of exiting.
        if gui_mode or args.login:
            try:
                config = Config(args.config)
            except ConfigError as e2:
                logger.info(str(e2))
                return 0
        else:
            logger.info(str(e))
            return 0

    if args.history:
        history_logger = VoteHistoryLogger(config.log_dir)
        print_history(history_logger)
        return 0

    if args.login:
        return run_login(config)

    if gui_mode:
        return run_gui(config)

    return run_cli(config, auto_mode=args.auto)


if __name__ == '__main__':
    sys.exit(main())
