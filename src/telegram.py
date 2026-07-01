"""Telegram Bot API alert sender."""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAlert:
    """Sends alert messages via the Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def _base_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.bot_token}"

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a single message.  Returns ``True`` on success."""
        url = f"{self._base_url}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                logger.debug("Telegram message sent (%d chars)", len(text))
                return True
            logger.error(
                "Telegram API error %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        except requests.RequestException as exc:
            logger.error("Failed to send Telegram message: %s", exc)
            return False

    def test_connection(self) -> bool:
        """Send a test message to verify the bot token and chat ID."""
        return self.send_message("✅ <b>IP Access Monitor</b>\nTest message — alerts are configured correctly.")

    # ------------------------------------------------------------------ #
    #  Pre-formatted alert helpers                                        #
    # ------------------------------------------------------------------ #

    def alert_down(
        self,
        ip: str,
        total_nodes: int,
        permanent_link: str,
    ) -> bool:
        msg = (
            f"\U0001F534 <b>DOWN</b>: <code>{ip}</code>\n"
            f"No location could ping this IP.\n"
            f"Checked from <b>{total_nodes}</b> nodes globally.\n"
            f'\n<a href="{permanent_link}">View full report</a>'
        )
        return self.send_message(msg)

    def alert_iran_only(
        self,
        ip: str,
        iran_nodes_ok: list[str],
        global_nodes_failed: list[str],
        permanent_link: str,
    ) -> bool:
        failed_str = ", ".join(global_nodes_failed) if global_nodes_failed else "none"
        msg = (
            f"\U0001F7E1 <b>IRAN-ONLY</b>: <code>{ip}</code>\n"
            f"Only Iranian nodes can reach this IP.\n"
            f"Iran nodes OK: <b>{len(iran_nodes_ok)}</b>\n"
            f"Global nodes failed: <b>{len(global_nodes_failed)}</b> ({failed_str})\n"
            f'\n<a href="{permanent_link}">View full report</a>'
        )
        return self.send_message(msg)

    def alert_degraded(
        self,
        ip: str,
        ok_count: int,
        total_count: int,
        pct: float,
        permanent_link: str,
    ) -> bool:
        msg = (
            f"\U0001F7E0 <b>DEGRADED</b>: <code>{ip}</code>\n"
            f"Only <b>{ok_count}/{total_count}</b> nodes can ping ({pct:.0f}%).\n"
            f'\n<a href="{permanent_link}">View full report</a>'
        )
        return self.send_message(msg)
