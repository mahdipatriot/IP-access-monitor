#!/usr/bin/env python3
"""IP Access Monitor

Monitors a list of IPs via the check-host.net ping API and sends Telegram
alerts when an IP is down globally or only accessible from Iran.

Usage:
    python3 monitor.py                          # continuous monitoring loop
    python3 monitor.py --once                   # single check cycle and exit
    python3 monitor.py --snooze 1.2.3.4 60      # snooze IP for 60 min
    python3 monitor.py --unsnooze 1.2.3.4       # remove snooze for IP
    python3 monitor.py --status                 # show current status
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from src.bot import TelegramBot
from src.checkhost_api import CheckHostAPI, CheckHostError
from src.evaluator import Condition, ResultEvaluator
from src.nodes import NodeManager
from src.snooze import SnoozeManager
from src.telegram import TelegramAlert

# --------------------------------------------------------------------------- #
#  Logging setup                                                               #
# --------------------------------------------------------------------------- #

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
logger = logging.getLogger("monitor")


# --------------------------------------------------------------------------- #
#  Configuration                                                               #
# --------------------------------------------------------------------------- #

def load_config() -> dict[str, Any]:
    load_dotenv()
    chat_ids_raw = os.getenv("TELEGRAM_CHAT_ID", "")
    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]

    config = {
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_ids": chat_ids,
        "check_interval": int(os.getenv("CHECK_INTERVAL", "120")),
        "result_wait": int(os.getenv("RESULT_WAIT", "5")),
        "node_cache_ttl": int(os.getenv("NODE_CACHE_TTL", "86400")),
        "max_nodes": int(os.getenv("MAX_NODES", "20")),
        "alert_threshold": float(os.getenv("ALERT_THRESHOLD", "0.7")),
        "snooze_minutes": int(os.getenv("SNOOZE_MINUTES", "30")),
        "priority_countries": {
            c.strip().lower()
            for c in os.getenv("PRIORITY_COUNTRIES", "ir,de").split(",")
            if c.strip()
        },
    }
    if not config["telegram_bot_token"] or not config["telegram_chat_ids"]:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)
    return config


def load_ips(ips_file: Path | None = None) -> list[str]:
    """Load IPs from ips.txt, ignoring comments and blank lines."""
    if ips_file is None:
        ips_file = Path(__file__).resolve().parent / "ips.txt"
    if not ips_file.exists():
        logger.error("ips.txt not found at %s", ips_file)
        sys.exit(1)

    ips: list[str] = []
    for line in ips_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ips.append(line)

    if not ips:
        logger.warning("No IPs found in %s — nothing to monitor", ips_file)
    else:
        logger.info("Loaded %d IP(s) to monitor: %s", len(ips), ", ".join(ips))
    return ips


# --------------------------------------------------------------------------- #
#  Status provider (shared by bot /status and CLI --status)                   #
# --------------------------------------------------------------------------- #

def make_status_provider(
    snooze: SnoozeManager,
    ips: list[str],
) -> Callable[[str], str]:
    """Return a callable that produces a status text for the bot /status command."""
    def _status(chat_id: str) -> str:
        lines = ["<b>IP Access Monitor — Status</b>\n"]
        for ip in ips:
            cond = snooze.get_last_condition(ip) or "unknown"
            icon = {"ok": "✅", "down": "🔴", "iran_only": "🟡", "degraded": "🟠"}.get(cond, "❓")
            snoozed = " (snoozed by you)" if snooze.is_snoozed(ip, chat_id) else ""
            lines.append(f"  {icon} <code>{ip}</code> — {cond}{snoozed}")
        return "\n".join(lines)
    return _status


# --------------------------------------------------------------------------- #
#  Main monitoring loop                                                        #
# --------------------------------------------------------------------------- #

def _check_single_ip(
    ip: str,
    api: CheckHostAPI,
    selected_nodes: list[str],
    evaluator: ResultEvaluator,
    telegram: TelegramAlert,
    snooze: SnoozeManager,
    result_wait: float,
    snooze_minutes: int,
) -> None:
    """Check a single IP, evaluate, and send alerts if needed."""
    try:
        logger.info("Starting ping check for %s", ip)
        response = api.start_ping_check(ip, nodes=selected_nodes)

        if not response.get("ok"):
            logger.error("check-ping failed for %s: %s", ip, response)
            return

        request_id = response["request_id"]
        permanent_link = response.get("permanent_link", "")

        # Poll until results are ready
        results = api.poll_until_complete(
            request_id,
            wait=result_wait,
            max_polls=6,
        )

        # Evaluate
        ev = evaluator.evaluate(ip, results, permanent_link)

        # Check for recovery (non-OK → OK)
        last_cond = snooze.get_last_condition(ip)
        if last_cond and last_cond != "ok" and ev.condition == Condition.OK:
            telegram.alert_recovery(ip, len(ev.ok_nodes), ev.total_nodes)
            logger.info("Recovery detected for %s — cleared all snoozes", ip)

        # Update last condition
        snooze.set_last_condition(ip, ev.condition.value)

        # Alert based on condition (telegram handles per-user snooze skipping)
        if ev.condition == Condition.DOWN:
            telegram.alert_down(ip, ev.total_nodes, permanent_link, snooze_minutes)
        elif ev.condition == Condition.IRAN_ONLY:
            telegram.alert_iran_only(
                ip,
                ev.iran_ok,
                ev.global_failed,
                permanent_link,
                snooze_minutes,
            )
        elif ev.condition == Condition.DEGRADED:
            telegram.alert_degraded(
                ip,
                len(ev.ok_nodes),
                ev.total_nodes,
                ev.ok_pct,
                permanent_link,
                snooze_minutes,
            )
        # Condition.OK → no alert (recovery already handled above)

    except CheckHostError as exc:
        logger.error("API error for %s: %s", ip, exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error checking %s: %s", ip, exc)


def run_cycle(
    ips: list[str],
    api: CheckHostAPI,
    node_manager: NodeManager,
    evaluator: ResultEvaluator,
    telegram: TelegramAlert,
    snooze: SnoozeManager,
    result_wait: float,
    snooze_minutes: int,
) -> None:
    """Run a single monitoring cycle: check all IPs in parallel, evaluate, alert."""

    # Select nodes once for the whole cycle
    selected_nodes = node_manager.select_nodes()
    if not selected_nodes:
        logger.error("No nodes available — skipping cycle")
        return

    # Clean up expired snoozes
    snooze.cleanup()

    # Check all IPs in parallel (max 10 concurrent)
    max_workers = min(len(ips), 10)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ip-check") as executor:
        futures = {
            executor.submit(
                _check_single_ip,
                ip,
                api,
                selected_nodes,
                evaluator,
                telegram,
                snooze,
                result_wait,
                snooze_minutes,
            ): ip
            for ip in ips
        }
        for future in as_completed(futures):
            ip = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                logger.exception("IP check failed for %s: %s", ip, exc)

    # Save snooze state once at end of cycle (batch save)
    snooze.save()


# --------------------------------------------------------------------------- #
#  CLI handlers for snooze / status                                           #
# --------------------------------------------------------------------------- #

def handle_cli_snooze(snooze: SnoozeManager, ip: str, minutes: int) -> None:
    snooze.snooze(ip, minutes, chat_id="cli")
    print(f"✅ Snoozed {ip} for {minutes} min (all CLI users)")
    sys.exit(0)


def handle_cli_unsnooze(snooze: SnoozeManager, ip: str) -> None:
    snooze.unsnooze_all(ip)
    print(f"✅ Removed snooze for {ip} (all users)")
    sys.exit(0)


def handle_cli_status(snooze: SnoozeManager, ips: list[str]) -> None:
    print("\nIP Access Monitor — Status\n")
    for ip in ips:
        cond = snooze.get_last_condition(ip) or "unknown"
        icon = {"ok": "✅", "down": "🔴", "iran_only": "🟡", "degraded": "🟠"}.get(cond, "❓")
        snoozed = " (snoozed by someone)" if snooze.is_snoozed_any(ip) else ""
        print(f"  {icon} {ip} — {cond}{snoozed}")
    sys.exit(0)


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="IP Access Monitor")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    parser.add_argument("--snooze", metavar="IP", help="Snooze alerts for an IP")
    parser.add_argument("--unsnooze", metavar="IP", help="Remove snooze for an IP")
    parser.add_argument("--status", action="store_true", help="Show current status and exit")
    parser.add_argument("--minutes", type=int, default=60, help="Snooze duration in minutes (default 60)")
    args = parser.parse_args()

    config = load_config()
    snooze = SnoozeManager()

    # CLI snooze / unsnooze / status (operate on persistent state, no monitoring loop)
    if args.snooze:
        handle_cli_snooze(snooze, args.snooze, args.minutes)
    if args.unsnooze:
        handle_cli_unsnooze(snooze, args.unsnooze)
    if args.status:
        ips = load_ips()
        handle_cli_status(snooze, ips)

    ips = load_ips()
    if not ips:
        return

    # Initialise components
    api = CheckHostAPI()
    node_manager = NodeManager(
        api,
        cache_ttl=config["node_cache_ttl"],
        max_global_nodes=config["max_nodes"],
        priority_countries=config["priority_countries"],
    )
    evaluator = ResultEvaluator(
        node_manager,
        threshold=config["alert_threshold"],
        priority_countries=config["priority_countries"],
    )
    telegram = TelegramAlert(
        bot_token=config["telegram_bot_token"],
        chat_ids=config["telegram_chat_ids"],
        snooze=snooze,
    )

    # Start Telegram bot (commands + ack button callbacks)
    status_provider = make_status_provider(snooze, ips)
    bot = TelegramBot(
        bot_token=config["telegram_bot_token"],
        authorized_chat_ids=config["telegram_chat_ids"],
        snooze_manager=snooze,
        status_provider=status_provider,
    )
    bot.start()

    logger.info(
        "Monitor started — %d IP(s), %d chat(s), interval=%ds, threshold=%.0f%%",
        len(ips),
        len(config["telegram_chat_ids"]),
        config["check_interval"],
        config["alert_threshold"] * 100,
    )

    if args.once:
        run_cycle(
            ips, api, node_manager, evaluator, telegram, snooze,
            config["result_wait"], config["snooze_minutes"],
        )
        bot.stop()
        return

    while True:
        cycle_start = time.time()
        try:
            run_cycle(
                ips, api, node_manager, evaluator, telegram, snooze,
                config["result_wait"], config["snooze_minutes"],
            )
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            bot.stop()
            break
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle failed: %s", exc)

        elapsed = time.time() - cycle_start
        sleep_time = max(0, config["check_interval"] - elapsed)
        logger.info("Cycle complete (%.1fs) — sleeping %.0fs", elapsed, sleep_time)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
