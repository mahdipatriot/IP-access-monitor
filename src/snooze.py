"""Snooze state management.

Tracks which IPs are snoozed (suppress alerts) and the last known condition
of each IP (for recovery detection).  State is persisted to a JSON file so
that snoozes survive restarts.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "snooze_state.json"


class SnoozeManager:
    """Manages snooze state and last-known conditions for monitored IPs."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or DEFAULT_STATE_PATH
        self._state: dict[str, Any] = {"snooze": {}, "last_conditions": {}}
        self.load()

    # ------------------------------------------------------------------ #
    #  Persistence                                                        #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state.setdefault("snooze", {})
            self._state.setdefault("last_conditions", {})
            logger.debug("Loaded snooze state from %s", self.state_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load snooze state: %s", exc)
            self._state = {"snooze": {}, "last_conditions": {}}

    def save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Failed to save snooze state: %s", exc)

    # ------------------------------------------------------------------ #
    #  Snooze operations                                                  #
    # ------------------------------------------------------------------ #

    def is_snoozed(self, ip: str) -> bool:
        """Return ``True`` if *ip* is currently snoozed."""
        self._cleanup_ip(ip)
        return ip in self._state["snooze"]

    def snooze(self, ip: str, minutes: int) -> None:
        """Snooze alerts for *ip* for *minutes* minutes."""
        expiry = time.time() + minutes * 60
        self._state["snooze"][ip] = expiry
        self.save()
        logger.info("Snoozed %s for %d min (until %s)", ip, minutes, time.ctime(expiry))

    def unsnooze(self, ip: str) -> None:
        """Remove snooze for *ip* immediately."""
        if ip in self._state["snooze"]:
            del self._state["snooze"][ip]
            self.save()
            logger.info("Removed snooze for %s", ip)

    def get_snoozed_list(self) -> list[dict[str, Any]]:
        """Return a list of snoozed IPs with remaining time."""
        now = time.time()
        result: list[dict[str, Any]] = []
        for ip, expiry in list(self._state["snooze"].items()):
            remaining = expiry - now
            if remaining <= 0:
                del self._state["snooze"][ip]
                continue
            result.append({
                "ip": ip,
                "remaining_min": round(remaining / 60, 1),
            })
        if result:
            self.save()
        return result

    # ------------------------------------------------------------------ #
    #  Condition tracking (for recovery detection)                        #
    # ------------------------------------------------------------------ #

    def get_last_condition(self, ip: str) -> str | None:
        return self._state["last_conditions"].get(ip)

    def set_last_condition(self, ip: str, condition: str) -> None:
        self._state["last_conditions"][ip] = condition
        self.save()

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _cleanup_ip(self, ip: str) -> None:
        """Remove expired snooze entry for *ip* if present."""
        expiry = self._state["snooze"].get(ip)
        if expiry is not None and expiry <= time.time():
            del self._state["snooze"][ip]
            self.save()
            logger.debug("Snooze expired for %s", ip)

    def cleanup(self) -> None:
        """Remove all expired snooze entries."""
        now = time.time()
        expired = [ip for ip, exp in self._state["snooze"].items() if exp <= now]
        for ip in expired:
            del self._state["snooze"][ip]
        if expired:
            self.save()
            logger.debug("Cleaned up %d expired snooze(s)", len(expired))
