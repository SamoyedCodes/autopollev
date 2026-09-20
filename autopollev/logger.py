"""
Logging & vote-history module.

Two channels:
1. Console log — colored, real-time status output.
2. Vote history file — written as JSONL to logs/vote_history.jsonl.
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime

from .config import app_dir


def _now() -> datetime:
    """Current time in the machine's local timezone (tz-aware)."""
    return datetime.now().astimezone()


class ColorFormatter(logging.Formatter):
    """Colored console log formatter."""

    COLORS = {
        logging.DEBUG: "\033[90m",      # grey
        logging.INFO: "\033[92m",       # green
        logging.WARNING: "\033[93m",    # yellow
        logging.ERROR: "\033[91m",      # red
        logging.CRITICAL: "\033[95m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        # Timestamp in grey
        timestamp = _now().strftime("%H:%M:%S")
        prefix = f"\033[90m[{timestamp}]\033[0m {color}"
        message = super().format(record)
        return f"{prefix}{message}{self.RESET}"


def setup_logger(name: str = "autopollev", level: int = logging.INFO) -> logging.Logger:
    """
    Set up and return a console logger with colored output.

    :param name: logger name
    :param level: logging level
    :return: the configured logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # prevent duplicate log lines

    # Avoid adding a handler more than once
    if not logger.handlers:
        # sys.stdout is None in a windowed (console-less) build.
        handler = (
            logging.StreamHandler(sys.stdout) if sys.stdout else logging.NullHandler()
        )
        handler.setLevel(level)
        handler.setFormatter(ColorFormatter("%(message)s"))
        logger.addHandler(handler)

        # A windowed build has no console at all, so without this a bug report
        # comes with nothing attached. Debug level: the diagnostics that make a
        # failed capture readable are logged below INFO.
        try:
            log_path = os.path.join(app_dir(), "logs", "autopollev.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
            )
            logger.addHandler(file_handler)
            logger.setLevel(min(level, logging.DEBUG))
        except OSError:  # read-only install dir: console logging still works
            pass

    return logger


class VoteHistoryLogger:
    """
    Vote history logger.

    Appends each vote to a log file in JSONL format.
    """

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        self.log_file = os.path.join(log_dir, "vote_history.jsonl")
        os.makedirs(log_dir, exist_ok=True)

    def record_vote(self, poll_id: str, poll_title: str,
                    selected_option: str, selected_option_id: int,
                    status: str, response_code: int):
        """
        Record a single vote.

        :param poll_id: poll unique identifier
        :param poll_title: poll title
        :param selected_option: text of the selected option
        :param selected_option_id: id of the selected option
        :param status: vote status (success/failed)
        :param response_code: HTTP response code
        """
        record = {
            "timestamp": _now().isoformat(),
            "poll_id": poll_id,
            "poll_title": poll_title,
            "selected_option": selected_option,
            "selected_option_id": selected_option_id,
            "status": status,
            "response_code": response_code,
        }

        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def get_history(self, limit: int = 50) -> list:
        """
        Read the most recent vote history records.

        :param limit: maximum number of records to return
        :return: list of vote records (newest first)
        """
        if not os.path.exists(self.log_file):
            return []

        records = []
        with open(self.log_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # A crash mid-append leaves a torn last line. Skip it
                    # rather than losing every record behind it.
                    continue

        # Return the last N records, newest first
        return records[-limit:][::-1]

    def get_stats(self) -> dict:
        """Return vote statistics."""
        history = self.get_history(limit=999999)
        total = len(history)
        success = sum(1 for r in history if r.get("status") == "success")
        failed = total - success
        return {
            "total": total,
            "success": success,
            "failed": failed,
        }
