"""Telegram Bot API alert sender.

Supports multiple chat IDs and inline keyboard buttons for acknowledge-based
snoozing.
"""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

ACK_KEYBOARD: dict[str, Any] = {
    "inline_keyboard": [[
        {"text": "\U0001F507 Acknowledge (30 min)", "callback_data": "ack"},
    ]],
}


class TelegramAlert:
    """Sends alert messages via the Telegram Bot API to multiple recipients."""

    def __init__(self, bot_token: str, chat_ids: list[str], timeout: int = 15) -> None:
        self.bot_token = bot_token
        self.chat_ids = chat_ids
        self.timeout = timeout

    @property
    def _base_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.bot_token}"

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Send a message to all configured chat IDs.

        Returns ``True`` if at least one delivery succeeded.
        """
        url = f"{self._base_url}/sendMessage"
        all_ok = True

        for chat_id in self.chat_ids:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    logger.debug("Telegram message sent to %s (%d chars)", chat_id, len(text))
                else:
                    logger.error(
                        "Telegram API error %d for chat %s: %s",
                        resp.status_code,
                        chat_id,
                        resp.text[:200],
                    )
                    all_ok = False
            except requests.RequestException as exc:
                logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
                all_ok = False

        return all_ok

    def test_connection(self) -> bool:
        """Send a test message to all configured chat IDs."""
        return self.send_message(
            "✅ <b>IP Access Monitor</b>\nTest message — alerts are configured correctly."
        )

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
        return self.send_message(msg, reply_markup=keyboard)

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
        return self.send_message(msg, reply_markup=keyboard)

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
        return self.send_message(msg, reply_markup=keyboard)

    def alert_recovery(
        self,
        ip: str,
        ok_count: int,
        total_count: int,
    ) -> bool:
        msg = (
            f"\u2705 <b>RECOVERED</b>: <code>{ip}</code>\n"
            f"Back online — <b>{ok_count}/{total_count}</b> nodes can ping now."
        )
        return self.send_message(msg)

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
