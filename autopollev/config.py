"""
Configuration management module.

Loads, saves, and validates the config.json file. On first run it
auto-generates a template with default values.
"""

import json
import os
import sys
from uuid import uuid4


# Default configuration template
DEFAULT_CONFIG = {
    "host": "your-presenter-id",
    "cookies": {
        "polleverywhere_session_id": "<captured automatically by the login browser>"
    },
    "poll_interval": 5,
    "answer_delay": 2,
    "user_choice_timeout": 30,
    "log_dir": "logs"
}


def app_dir() -> str:
    """Directory holding config.json, logs and the login profile.

    A PyInstaller build unpacks the package into a temp dir that is wiped on
    exit, so frozen runs anchor on the executable instead of __file__.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(app_dir(), "config.json")


class ConfigError(Exception):
    """Configuration error."""


class Config:
    """Manages AutoPollEv configuration."""

    def __init__(self, config_path: str = None):
        self.config_path = config_path or CONFIG_PATH
        self._data = {}
        self.load()

    def load(self):
        """Load the config file. If it does not exist, generate a default template."""
        if not os.path.exists(self.config_path):
            self._create_default()
            raise ConfigError(
                f"Config file generated: {self.config_path}\n"
                f"Recommended: run `python main.py --gui` and click "
                f"'Log in', or run `python main.py --login` "
                f"to capture and save the session cookie automatically.\n"
                f"Manual: log in to pollev.com in your browser → F12 → "
                f"Application → Cookies → copy the value of "
                f"polleverywhere_session_id into config.json."
            )

        with open(self.config_path, 'r', encoding='utf-8') as f:
            try:
                self._data = json.load(f)
            except json.JSONDecodeError as e:
                # Not a ConfigError, this would reach a windowed build as an
                # uncaught traceback with no console to print it to.
                raise ConfigError(
                    f"{self.config_path} is not valid JSON: {e}\n"
                    f"Fix the file, or delete it to start from a fresh template."
                )

        self._validate()

    def _create_default(self):
        """Write the default config file."""
        os.makedirs(os.path.dirname(self.config_path) or '.', exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)

    def _validate(self):
        """Validate that required fields are present."""
        required_keys = ["host", "cookies", "poll_interval", "answer_delay", "log_dir"]
        for key in required_keys:
            if key not in self._data:
                raise ConfigError(f"Config is missing a required field: {key}")

        # Duck typing turns "5" into 5555555555 ticks downstream, which reads
        # as a hang rather than a bad setting. Reject it here instead.
        for key in ("poll_interval", "answer_delay", "user_choice_timeout"):
            if key in self._data:
                try:
                    float(self._data[key])
                except (TypeError, ValueError):
                    raise ConfigError(
                        f"Config field '{key}' must be a number, "
                        f"got {self._data[key]!r}."
                    )

        cookies = self._data.get("cookies", {})
        if not cookies.get("polleverywhere_session_id") and not cookies.get("pe_auth_token"):
            raise ConfigError(
                "No valid authentication cookie (polleverywhere_session_id) found.\n"
                "Please edit config.json and fill in the value captured from the browser."
            )

    def save(self):
        """Save the configuration, atomically — a torn write loses the cookie."""
        tmp_path = f"{self.config_path}.tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.config_path)

    @property
    def host(self) -> str:
        return self._data["host"]

    @property
    def cookies(self) -> dict:
        return self._data["cookies"]

    def _number(self, key: str, default: float) -> float:
        """Read a numeric setting, tolerating a quoted number in the file."""
        try:
            return float(self._data.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def poll_interval(self) -> float:
        return self._number("poll_interval", 5)

    @property
    def answer_delay(self) -> float:
        return self._number("answer_delay", 2)

    @property
    def user_choice_timeout(self) -> float:
        return self._number("user_choice_timeout", 30)

    @property
    def log_dir(self) -> str:
        # Relative log dirs follow the app, not the shell's working directory.
        return os.path.join(app_dir(), self._data.get("log_dir", "logs"))

    def update_cookie(self, key: str, value: str):
        """Update a single cookie value and save."""
        self._data["cookies"][key] = value
        self.save()

    def update_host(self, host: str) -> bool:
        """Validate, persist, and report whether the presenter host changed."""
        host = host.strip()
        if not host:
            raise ConfigError("Host cannot be empty.")
        if any(char.isspace() for char in host):
            raise ConfigError("Host cannot contain whitespace.")
        if "." in host or any(char in host for char in "/\\:?#"):
            raise ConfigError(
                "Enter the presenter ID only, not a URL or domain."
            )
        if host == self.host:
            return False

        previous_host = self.host
        self._data["host"] = host
        try:
            self.save()
        except Exception:
            self._data["host"] = previous_host
            raise
        return True

    def generate_visit_cookies(self) -> dict:
        """Generate pollev_visitor and pollev_visit UUID cookies."""
        return {
            "pollev_visitor": str(uuid4()),
            "pollev_visit": str(uuid4()),
        }

    def to_dict(self) -> dict:
        return self._data.copy()

    def __repr__(self):
        return f"Config(host='{self.host}', config_path='{self.config_path}')"
