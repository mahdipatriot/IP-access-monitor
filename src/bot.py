"""Telegram bot command handler.

Runs a background thread that polls Telegram ``getUpdates`` (long polling)
and handles:
  - Inline callback queries (acknowledge button on alerts)
  - Bot commands: /start, /snooze, /unsnooze, /status, /list, /help
"""

import logging
import threading
import time
from typing import Any

import requests

from .snooze import SnoozeManager

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT = 3  # short for fast response
API_TIMEOUT = 10  # seconds for regular API calls


class TelegramBot:
    """Background bot that listens for commands and callback queries."""

    def __init__(
        self,
        bot_token: str,
        authorized_chat_ids: list[str],
        snooze_manager: SnoozeManager,
        status_provider: Any | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.authorized_chat_ids = set(authorized_chat_ids)
        self.snooze = snooze_manager
        self.status_provider = status_provider
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session = requests.Session()

    @property
    def _base_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.bot_token}"

    def _api_call(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: int | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self._base_url}/{method}"
        t0 = time.time()
        try:
            resp = self._session.post(url, json=payload, timeout=timeout or API_TIMEOUT)
            elapsed = time.time() - t0
            if resp.status_code == 200:
                logger.debug("Bot API %s OK (%.1fs)", method, elapsed)
                return resp.json()
            logger.error("Bot API error %d for %s: %s", resp.status_code, method, resp.text[:200])
        except requests.RequestException as exc:
            logger.error("Bot API request failed for %s: %s", method, exc)
        return None

    # ------------------------------------------------------------------ #
    #  Background thread                                                  #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the bot polling thread."""
        logger.info("Clearing any existing Telegram webhook...")
        self._api_call("deleteWebhook", {"drop_pending_updates": True})

        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-bot")
        self._thread.start()
        logger.info("Telegram bot thread started — listening for commands")

    def stop(self) -> None:
        """Signal the bot thread to stop."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_updates()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Bot polling error: %s", exc)
                time.sleep(2)

    def _poll_updates(self) -> None:
        result = self._api_call("getUpdates", {
            "offset": self._offset,
            "timeout": LONG_POLL_TIMEOUT,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=LONG_POLL_TIMEOUT + 5)

        if not result or not result.get("ok"):
            return

        updates = result.get("result", [])
        for update in updates:
            self._offset = update["update_id"] + 1

            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])

    # ------------------------------------------------------------------ #
    #  Callback handler (acknowledge button)                              #
    # ------------------------------------------------------------------ #

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id", "")
        data = callback.get("data", "")
        from_user = callback.get("from", {})
        chat_id = str(from_user.get("id", ""))

        if chat_id not in self.authorized_chat_ids:
            self._api_call("answerCallbackQuery", {
                "callback_query_id": query_id,
                "text": "Not authorized",
            })
            return

        # Parse callback data: "ack:<IP>:<minutes>" or "ack:<IP>"
        parts = data.split(":")
        if len(parts) < 2 or parts[0] != "ack":
            self._api_call("answerCallbackQuery", {
                "callback_query_id": query_id,
                "text": "Unknown action",
            })
            return

        ip = parts[1]
        minutes = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30

        self.snooze.snooze(ip, minutes)
        self._api_call("answerCallbackQuery", {
            "callback_query_id": query_id,
            "text": f"Snoozed {ip} for {minutes} min",
        })

        msg_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", chat_id))
        self._api_call("sendMessage", {
            "chat_id": msg_chat_id,
            "text": f"Alerts for <code>{ip}</code> snoozed for {minutes} min.\n"
                    f"Use /unsnooze {ip} to cancel early.",
            "parse_mode": "HTML",
        })

    # ------------------------------------------------------------------ #
    #  Message handler (bot commands)                                     #
    # ------------------------------------------------------------------ #

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text")

        if text is None:
            return

        text = text.strip()
        logger.info("Bot: message from %s: %r", chat_id, text)

        if chat_id not in self.authorized_chat_ids:
            logger.debug("Bot: chat %s not in authorized %s", chat_id, self.authorized_chat_ids)
            return

        if not text.startswith("/"):
            return

        parts = text.split()
        raw_cmd = parts[0]
        command = raw_cmd.lower().split("@")[0]
        args = parts[1:]

        logger.info("Bot: command=%r args=%r", command, args)

        handler = self._get_command_handler(command)

        if handler:
            reply = handler(args)
        else:
            reply = self._cmd_help([])

        self._api_call("sendMessage", {
            "chat_id": chat_id,
            "text": reply,
            "parse_mode": "HTML",
        })

    def _get_command_handler(self, command: str):
        handlers = {
            "/start": self._cmd_help,
            "/snooze": self._cmd_snooze,
            "/unsnooze": self._cmd_unsnooze,
            "/status": self._cmd_status,
            "/list": self._cmd_list,
            "/help": self._cmd_help,
        }
        return handlers.get(command)

    # ------------------------------------------------------------------ #
    #  Command handlers                                                   #
    # ------------------------------------------------------------------ #

    def _cmd_snooze(self, args: list[str]) -> str:
        if not args:
            return "Usage: /snooze &lt;IP&gt; [minutes]\nExample: /snooze 1.2.3.4 60"
        ip = args[0]
        minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 60
        self.snooze.snooze(ip, minutes)
        return f"Snoozed <code>{ip}</code> for {minutes} min."

    def _cmd_unsnooze(self, args: list[str]) -> str:
        if not args:
            return "Usage: /unsnooze &lt;IP&gt;\nExample: /unsnooze 1.2.3.4"
        ip = args[0]
        self.snooze.unsnooze(ip)
        return f"Removed snooze for <code>{ip}</code>."

    def _cmd_status(self, args: list[str]) -> str:
        if self.status_provider:
            return self.status_provider()
        return "Status not available."

    def _cmd_list(self, args: list[str]) -> str:
        snoozed = self.snooze.get_snoozed_list()
        if not snoozed:
            return "No IPs are currently snoozed."
        lines = ["<b>Snoozed IPs:</b>"]
        for item in snoozed:
            lines.append(f"  - <code>{item['ip']}</code> - {item['remaining_min']} min remaining")
        return "\n".join(lines)

    def _cmd_help(self, args: list[str]) -> str:
        return (
            "<b>IP Access Monitor - Commands</b>\n\n"
            "/snooze &lt;IP&gt; [minutes] - Snooze alerts for an IP (default 60 min)\n"
            "/unsnooze &lt;IP&gt; - Remove snooze for an IP\n"
            "/status - Show all monitored IPs and their last condition\n"
            "/list - Show currently snoozed IPs\n"
            "/help - Show this help message\n\n"
            "You can also tap the Acknowledge button on any alert to snooze for 30 min."
        )
