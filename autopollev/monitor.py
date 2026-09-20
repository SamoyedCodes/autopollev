"""
Poll monitoring module.

Polls the Firehose endpoint to detect new polls, and hands a detected poll
off to the Voter.
"""

import hashlib
import json
import threading
import time
from typing import Any, Optional

import requests

from .endpoints import ENDPOINTS
from .auth import Auth, AuthError, CookieExpiredError, json_or_raise
from .logger import setup_logger
from .i18n import _

logger = setup_logger("autopollev.monitor")


class PollMonitor:
    """
    PollEv poll monitor.

    Continuously polls the Firehose endpoint, detects new polls, and returns
    poll info.
    """

    def __init__(self, auth: Auth, poll_interval: float = 5):
        """
        Initialize the monitor.

        :param auth: an authenticated Auth instance
        :param poll_interval: polling interval in seconds
        """
        self.auth = auth
        self.poll_interval = poll_interval
        self._running = False
        self._answered_polls = set()
        self._in_flight_polls = set()
        self._answered_count = 0
        self._active_poll_uid: Optional[str] = None
        self._firehose_token = None
        self._last_poll_uid: Optional[str] = None
        self._last_poll_time: float = 0
        self._retry_count = 0
        self._max_retries = 5
        self._last_unrecognized_payload: Optional[str] = None
        self._last_bad_response: Optional[str] = None
        self._state_lock = threading.Lock()

    # Keep the parser deliberately small, but accept a bounded set of common
    # wrapper names so a transport envelope change does not make a valid poll
    # look like "no active poll".
    _WRAPPER_KEYS = (
        "message",
        "messages",
        "data",
        "activity",
        "poll",
        "payload",
        "current",
        "result",
    )
    _UID_KEYS = ("uid", "poll_uid", "pollUid", "permalink")
    _TYPE_KEYS = ("type", "poll_type", "pollType", "activity_type")
    _MAX_PARSE_DEPTH = 6

    @staticmethod
    def _timestamp() -> int:
        return round(time.time() * 1000)

    @classmethod
    def _decode_json_string(cls, value: Any) -> Any:
        """Decode a JSON-encoded Firehose value, including double encoding."""
        decoded = value
        for _ in range(2):
            if not isinstance(decoded, str):
                break
            candidate = decoded.strip()
            if not candidate:
                return None
            try:
                decoded = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                return None
        return decoded

    @classmethod
    def _extract_poll_info(cls, payload: Any, depth: int = 0) -> Optional[dict]:
        """Return normalized poll metadata from a supported Firehose envelope.

        The legacy response stores a JSON string in ``message``. Newer or
        proxied responses may expose that value as an object, wrap it in
        ``data``/``activity``/``poll``, or return a list of recent messages.
        """
        if depth > cls._MAX_PARSE_DEPTH:
            return None

        payload = cls._decode_json_string(payload)

        if isinstance(payload, list):
            # Firehose message arrays are chronological; prefer the newest
            # valid poll while still tolerating an undocumented ordering.
            for item in reversed(payload):
                poll_info = cls._extract_poll_info(item, depth + 1)
                if poll_info:
                    return poll_info
            return None

        if not isinstance(payload, dict):
            return None

        uid = next(
            (payload.get(key) for key in cls._UID_KEYS if payload.get(key)),
            None,
        )
        if uid is not None:
            poll_info = dict(payload)
            poll_info["uid"] = str(uid)

            if not poll_info.get("type"):
                poll_type = next(
                    (
                        payload.get(key)
                        for key in cls._TYPE_KEYS
                        if payload.get(key)
                    ),
                    "unknown",
                )
                poll_info["type"] = poll_type
            return poll_info

        for key in cls._WRAPPER_KEYS:
            if key not in payload:
                continue
            poll_info = cls._extract_poll_info(payload[key], depth + 1)
            if not poll_info:
                continue

            # Preserve event-level type metadata when the nested poll object
            # contains only its UID/permalink.
            if poll_info.get("type") in (None, "", "unknown"):
                poll_type = next(
                    (
                        payload.get(type_key)
                        for type_key in cls._TYPE_KEYS
                        if payload.get(type_key)
                    ),
                    None,
                )
                if poll_type:
                    poll_info["type"] = poll_type
            return poll_info

        return None

    @staticmethod
    def _payload_signature(payload: Any) -> str:
        """Build a stable SHA-256 signature for de-duplication."""
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            encoded = repr(payload)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _payload_summary(cls, payload: Any) -> str:
        """Describe payload structure without logging response values."""
        if isinstance(payload, dict):
            keys = sorted(str(key) for key in payload.keys())
            visible_keys = keys[:12]
            if len(keys) > len(visible_keys):
                visible_keys.append("...")
            wrapper_types = [
                f"{key}:{type(payload[key]).__name__}"
                for key in cls._WRAPPER_KEYS
                if key in payload
            ]
            summary = f"object keys={visible_keys}"
            if wrapper_types:
                summary += f" wrappers={wrapper_types}"
            return summary
        if isinstance(payload, list):
            return f"array length={len(payload)}"
        return f"type={type(payload).__name__}"

    @classmethod
    def _is_empty_firehose_payload(cls, payload: Any) -> bool:
        """Identify the normal "no active poll" Firehose response."""
        if payload in (None, "") or payload == []:
            return True
        if not isinstance(payload, dict):
            return False

        wrapper_values = [
            payload[key] for key in cls._WRAPPER_KEYS if key in payload
        ]
        return bool(wrapper_values) and all(
            value in (None, "") or value == [] for value in wrapper_values
        )

    def _init_firehose(self):
        """Initialize the Firehose token."""
        try:
            self._firehose_token = self.auth.get_firehose_token()
            if self._firehose_token:
                logger.info(_("monitor.firehose_ok"))
            else:
                logger.info(_("monitor.firehose_no_token"))
            self._retry_count = 0
        except CookieExpiredError:
            raise
        except Exception as e:
            logger.warning(_("monitor.firehose_fail", error=e))
            self._firehose_token = None

    def _clear_active_poll(self):
        """Reset per-activation state after the presenter closes a poll."""
        with self._state_lock:
            self._active_poll_uid = None
            self._answered_polls.clear()
            self._last_poll_uid = None
            self._last_poll_time = 0

    def check_new_poll(self) -> Optional[dict]:
        """
        Check whether there is a new poll.

        :return: a claimed poll dict {'uid': str, 'type': str, ...}, or None.
                 The caller must resolve a returned claim with mark_answered()
                 or mark_retryable().
        :raises CookieExpiredError: when the cookie is expired
        """
        if self._firehose_token:
            url = ENDPOINTS['firehose_with_token'].format(
                host=self.auth.host,
                token=self._firehose_token,
                timestamp=self._timestamp()
            )
        else:
            url = ENDPOINTS['firehose_no_token'].format(
                host=self.auth.host,
                timestamp=self._timestamp()
            )

        try:
            r = self.auth.session.get(url, timeout=5)

            # Check cookie validity
            self.auth.check_response_status(r)
            r.raise_for_status()

            data = json_or_raise(r, "firehose")
            self._retry_count = 0

            poll_info = self._extract_poll_info(data)
            if not poll_info:
                if self._is_empty_firehose_payload(data):
                    self._last_unrecognized_payload = None
                    self._clear_active_poll()
                    return None
                payload_signature = self._payload_signature(data)
                if payload_signature != self._last_unrecognized_payload:
                    logger.warning(
                        _(
                            "monitor.unrecognized_payload",
                            payload=self._payload_summary(data),
                        )
                    )
                    self._last_unrecognized_payload = payload_signature
                return None

            self._last_unrecognized_payload = None
            poll_uid = poll_info.get('uid')

            if not poll_uid:
                return None

            current_time = time.time()
            with self._state_lock:
                if poll_uid != self._active_poll_uid:
                    # Suppression applies only to the current activation. If
                    # the presenter switches polls (or later reactivates this
                    # UID), it must be eligible for detection again.
                    self._active_poll_uid = poll_uid
                    self._answered_polls.clear()

                if (
                    poll_uid in self._answered_polls
                    or poll_uid in self._in_flight_polls
                ):
                    return None

                # Throttle retries by question identity. Payload metadata may
                # change while the underlying poll remains the same.
                if (
                    poll_uid == self._last_poll_uid
                    and (current_time - self._last_poll_time) < 15
                ):
                    return None

                self._last_poll_uid = poll_uid
                self._last_poll_time = current_time
                self._in_flight_polls.add(poll_uid)

            return poll_info

        except CookieExpiredError:
            raise
        except AuthError as e:
            # Something answered that is not the API. Retrying will not help
            # and the run looks healthy otherwise, so say it once per message.
            if str(e) != self._last_bad_response:
                logger.warning(_("monitor.bad_response", error=e))
                self._last_bad_response = str(e)
            return None
        except requests.exceptions.ReadTimeout:
            # Firehose is a long poll. A timeout means no new message arrived,
            # not that the current activity ended.
            return None
        except (requests.RequestException, ValueError, TypeError, KeyError) as e:
            # Network error — retry with exponential backoff
            self._retry_count += 1
            logger.debug(
                "Firehose request/parse error (%s)", type(e).__name__
            )
            if self._retry_count >= self._max_retries:
                logger.warning(
                    _("monitor.retrying_firehose", n=self._max_retries)
                )
                self._init_firehose()
            return None

    def mark_answered(self, poll_uid: str):
        """Complete a successful claim and suppress duplicate processing."""
        with self._state_lock:
            self._in_flight_polls.discard(poll_uid)
            if poll_uid not in self._answered_polls:
                self._answered_polls.add(poll_uid)
                self._answered_count += 1

    def mark_retryable(self, poll_uid: str):
        """Release an unsuccessful poll attempt so it can be retried."""
        with self._state_lock:
            self._in_flight_polls.discard(poll_uid)

    def start(self):
        """Start monitoring (sets the flag; the actual loop is driven by main)."""
        self._running = True
        self._init_firehose()
        logger.info(_("monitor.started", host=self.auth.host, interval=self.poll_interval))

    def stop(self):
        """Stop monitoring."""
        self._running = False
        logger.info(_("monitor.stopped"))

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def answered_count(self) -> int:
        with self._state_lock:
            return self._answered_count
