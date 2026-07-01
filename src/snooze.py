"""Snooze state management — per-user.

Tracks which IPs are snoozed per chat_id (so one admin snoozing doesn't
affect others) and the last known condition of each IP (for recovery
detection).  State is persisted to a JSON file so snoozes survive restarts.

Thread-safe: all state access is guarded by a lock since the main monitoring
thread and the bot thread both read/write state.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "snooze_state.json"


class SnoozeManager:
    """Manages per-user snooze state and last-known conditions for monitored IPs."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or DEFAULT_STATE_PATH
        self._state: dict[str, Any] = {"snooze": {}, "last_conditions": {}}
        self._lock = threading.Lock()
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ #
    #  Persistence                                                        #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        with self._lock:
            if not self.state_path.exists():
                return
            try:
                self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
                self._state.setdefault("snooze", {})
                self._state.setdefault("last_conditions", {})
                self._dirty = False
                logger.debug("Loaded snooze state from %s", self.state_path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load snooze state: %s", exc)
                self._state = {"snooze": {}, "last_conditions": {}}

    def save(self) -> None:
        """Persist state to disk only if there are unsaved changes."""
        with self._lock:
            if not self._dirty:
                return
            try:
                self.state_path.write_text(
                    json.dumps(self._state, indent=2), encoding="utf-8"
                )
                self._dirty = False
            except OSError as exc:
                logger.warning("Failed to save snooze state: %s", exc)

    def _mark_dirty(self) -> None:
        """Must be called with lock held."""
        self._dirty = True

    # ------------------------------------------------------------------ #
    #  Per-user snooze operations                                         #
    # ------------------------------------------------------------------ #

    def is_snoozed(self, ip: str, chat_id: str) -> bool:
        """Return ``True`` if *ip* is currently snoozed for *chat_id*."""
        with self._lock:
            self._cleanup_ip(chat_id, ip)
            user_snooze = self._state["snooze"].get(chat_id, {})
            return ip in user_snooze

    def is_snoozed_any(self, ip: str) -> bool:
        """Return ``True`` if *ip* is snoozed by at least one user."""
        with self._lock:
            for chat_id in list(self._state["snooze"].keys()):
                self._cleanup_ip(chat_id, ip)
                if ip in self._state["snooze"].get(chat_id, {}):
                    return True
            return False

    def snooze(self, ip: str, minutes: int, chat_id: str) -> None:
        """Snooze alerts for *ip* for *minutes* minutes for *chat_id* only."""
        expiry = time.time() + minutes * 60
        with self._lock:
            self._state["snooze"].setdefault(chat_id, {})[ip] = expiry
            self._mark_dirty()
        self.save()
        logger.info("Snoozed %s for %s (%d min, until %s)", ip, chat_id, minutes, time.ctime(expiry))

    def unsnooze(self, ip: str, chat_id: str) -> None:
        """Remove snooze for *ip* for *chat_id* only."""
        with self._lock:
            user_snooze = self._state["snooze"].get(chat_id, {})
            if ip in user_snooze:
                del user_snooze[ip]
                self._mark_dirty()
        self.save()
        logger.info("Removed snooze for %s (%s)", ip, chat_id)

    def unsnooze_all(self, ip: str) -> None:
        """Remove snooze for *ip* for ALL users (used on recovery)."""
        with self._lock:
            for chat_id in list(self._state["snooze"].keys()):
                user_snooze = self._state["snooze"][chat_id]
                if ip in user_snooze:
                    del user_snooze[ip]
                    self._mark_dirty()
        self.save()
        logger.info("Cleared snooze for %s (all users)", ip)

    def get_snoozed_list(self, chat_id: str) -> list[dict[str, Any]]:
        """Return a list of snoozed IPs with remaining time for *chat_id*."""
        now = time.time()
        removed = False
        with self._lock:
            user_snooze = self._state["snooze"].get(chat_id, {})
            result: list[dict[str, Any]] = []
            for ip, expiry in list(user_snooze.items()):
                remaining = expiry - now
                if remaining <= 0:
                    del user_snooze[ip]
                    removed = True
                    continue
                result.append({
                    "ip": ip,
                    "remaining_min": round(remaining / 60, 1),
                })
            if removed:
                self._mark_dirty()
        if removed:
            self.save()
        return result

    # ------------------------------------------------------------------ #
    #  Condition tracking (for recovery detection)                        #
    # ------------------------------------------------------------------ #

    def get_last_condition(self, ip: str) -> str | None:
        with self._lock:
            return self._state["last_conditions"].get(ip)

    def set_last_condition(self, ip: str, condition: str) -> None:
        """Mark condition as dirty — caller should call save() once at end of cycle."""
        with self._lock:
            self._state["last_conditions"][ip] = condition
            self._mark_dirty()

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _cleanup_ip(self, chat_id: str, ip: str) -> None:
        """Remove expired snooze entry for *ip* under *chat_id* if present.

        Must be called with the lock held.
        """
        user_snooze = self._state["snooze"].get(chat_id, {})
        expiry = user_snooze.get(ip)
        if expiry is not None and expiry <= time.time():
            del user_snooze[ip]
            self._mark_dirty()
            logger.debug("Snooze expired for %s (%s)", ip, chat_id)

    def cleanup(self) -> None:
        """Remove all expired snooze entries across all users."""
        now = time.time()
        with self._lock:
            for chat_id in list(self._state["snooze"].keys()):
                user_snooze = self._state["snooze"][chat_id]
                expired = [ip for ip, exp in user_snooze.items() if exp <= now]
                for ip in expired:
                    del user_snooze[ip]
                if expired:
                    self._mark_dirty()
        self.save()
        logger.debug("Cleaned up expired snooze(s)")
