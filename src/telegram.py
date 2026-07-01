"""Telegram Bot API alert sender.

Supports multiple chat IDs, per-user snooze skipping, and inline keyboard
buttons for acknowledge-based snoozing.
"""

import logging
from typing import Any

import requests

from .snooze import SnoozeManager

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAlert:
    """Sends alert messages via the Telegram Bot API to multiple recipients.

    When a ``SnoozeManager`` is provided, alert methods skip recipients who
    have snoozed the IP in question.  Recovery alerts are always sent to all
    recipients.
    """

    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str],
        snooze: SnoozeManager | None = None,
        timeout: int = 15,
    ) -> None:
        self.bot_token = bot_token
        self.chat_ids = chat_ids
        self.snooze = snooze
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def _base_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.bot_token}"

    # ------------------------------------------------------------------ #
    #  Core send (no snooze filtering — used for test, bot replies)       #
    # ------------------------------------------------------------------ #

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
    ) -> bool:
        """Send a message to all (or one) chat ID(s).

        Returns ``True`` if at least one delivery succeeded.
        """
        targets = [chat_id] if chat_id else self.chat_ids
        all_ok = True

        for cid in targets:
            payload: dict[str, Any] = {
                "chat_id": cid,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = self._session.post(
                    f"{self._base_url}/sendMessage",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    logger.debug("Telegram message sent to %s (%d chars)", cid, len(text))
                else:
                    logger.error(
                        "Telegram API error %d for chat %s: %s",
                        resp.status_code,
                        cid,
                        resp.text[:200],
                    )
                    all_ok = False
            except requests.RequestException as exc:
                logger.error("Failed to send Telegram message to %s: %s", cid, exc)
                all_ok = False

        return all_ok

    def test_connection(self) -> bool:
        """Send a test message to all configured chat IDs."""
        return self.send_message(
            "✅ <b>IP Access Monitor</b>\nTest message — alerts are configured correctly."
        )

    # ------------------------------------------------------------------ #
    #  Alert send (with per-user snooze skipping)                         #
    # ------------------------------------------------------------------ #

    def _send_alert(
        self,
        ip: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Send an alert to all chat IDs, skipping users who snoozed *ip*."""
        all_ok = True
        sent_count = 0

        for cid in self.chat_ids:
            if self.snooze and self.snooze.is_snoozed(ip, cid):
                logger.debug("Skipping alert for %s — snoozed by %s", ip, cid)
                continue

            payload: dict[str, Any] = {
                "chat_id": cid,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = self._session.post(
                    f"{self._base_url}/sendMessage",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    sent_count += 1
                    logger.debug("Alert sent to %s for %s", cid, ip)
                else:
                    logger.error(
                        "Telegram API error %d for chat %s: %s",
                        resp.status_code,
                        cid,
                        resp.text[:200],
                    )
                    all_ok = False
            except requests.RequestException as exc:
                logger.error("Failed to send alert to %s: %s", cid, exc)
                all_ok = False

        logger.info("Alert for %s sent to %d/%d recipients", ip, sent_count, len(self.chat_ids))
        return all_ok

    # ------------------------------------------------------------------ #
    #  Pre-formatted alert helpers (with inline ack button)               #
    # ------------------------------------------------------------------ #

    def alert_down(
        self,
        ip: str,
        total_nodes: int,
        permanent_link: str,
        snooze_minutes: int = 30,
    ) -> bool:
        msg = (
            f"\U0001F534 <b>DOWN</b>: <code>{ip}</code>\n"
            f"No location could ping this IP.\n"
            f"Checked from <b>{total_nodes}</b> nodes globally.\n"
            f'\n<a href="{permanent_link}">View full report</a>'
        )
        keyboard = self._ack_keyboard(ip, snooze_minutes)
        return self._send_alert(ip, msg, reply_markup=keyboard)

    def alert_iran_only(
        self,
        ip: str,
        iran_nodes_ok: list[str],
        global_nodes_failed: list[str],
        permanent_link: str,
        snooze_minutes: int = 30,
    ) -> bool:
        failed_str = ", ".join(global_nodes_failed) if global_nodes_failed else "none"
        msg = (
            f"\U0001F7E1 <b>IRAN-ONLY</b>: <code>{ip}</code>\n"
            f"Only Iranian nodes can reach this IP.\n"
            f"Iran nodes OK: <b>{len(iran_nodes_ok)}</b>\n"
            f"Global nodes failed: <b>{len(global_nodes_failed)}</b> ({failed_str})\n"
            f'\n<a href="{permanent_link}">View full report</a>'
        )
        keyboard = self._ack_keyboard(ip, snooze_minutes)
        return self._send_alert(ip, msg, reply_markup=keyboard)

    def alert_degraded(
        self,
        ip: str,
        ok_count: int,
        total_count: int,
        pct: float,
        permanent_link: str,
        snooze_minutes: int = 30,
    ) -> bool:
        msg = (
            f"\U0001F7E0 <b>DEGRADED</b>: <code>{ip}</code>\n"
            f"Only <b>{ok_count}/{total_count}</b> nodes can ping ({pct:.0f}%).\n"
            f'\n<a href="{permanent_link}">View full report</a>'
        )
        keyboard = self._ack_keyboard(ip, snooze_minutes)
        return self._send_alert(ip, msg, reply_markup=keyboard)

    def alert_recovery(
        self,
        ip: str,
        ok_count: int,
        total_count: int,
    ) -> bool:
        """Send recovery to ALL users (ignores snooze) and clear all snoozes."""
        msg = (
            f"\u2705 <b>RECOVERED</b>: <code>{ip}</code>\n"
            f"Back online — <b>{ok_count}/{total_count}</b> nodes can ping now."
        )
        result = self.send_message(msg)  # send_message = no snooze filtering
        if self.snooze:
            self.snooze.unsnooze_all(ip)
        return result

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ack_keyboard(ip: str, minutes: int) -> dict[str, Any]:
        """Build an inline keyboard with an acknowledge button for *ip*."""
        return {
            "inline_keyboard": [[
                {
                    "text": f"\U0001F507 Acknowledge ({minutes} min)",
                    "callback_data": f"ack:{ip}:{minutes}",
                },
            ]],
        }
