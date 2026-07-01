#!/usr/bin/env python3
"""IP Access Monitor

Monitors a list of IPs via the check-host.net ping API and sends Telegram
alerts when an IP is down globally or only accessible from Iran.

Usage:
    python3 monitor.py            # uses .env in the current directory
    python3 monitor.py --once     # run a single check cycle and exit
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.checkhost_api import CheckHostAPI, CheckHostError
from src.evaluator import Condition, ResultEvaluator
from src.nodes import NodeManager
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

def load_config() -> dict[str, str]:
    load_dotenv()
    config = {
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "check_interval": int(os.getenv("CHECK_INTERVAL", "120")),
        "result_wait": int(os.getenv("RESULT_WAIT", "5")),
        "node_cache_ttl": int(os.getenv("NODE_CACHE_TTL", "86400")),
        "max_nodes": int(os.getenv("MAX_NODES", "20")),
        "alert_threshold": float(os.getenv("ALERT_THRESHOLD", "0.7")),
        "priority_countries": {
            c.strip().lower()
            for c in os.getenv("PRIORITY_COUNTRIES", "ir,de").split(",")
            if c.strip()
        },
    }
    if not config["telegram_bot_token"] or not config["telegram_chat_id"]:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)
    return config


def load_ips(ips_file: Path = None) -> list[str]:
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
#  Main monitoring loop                                                        #
# --------------------------------------------------------------------------- #

def run_cycle(
    ips: list[str],
    api: CheckHostAPI,
    node_manager: NodeManager,
    evaluator: ResultEvaluator,
    telegram: TelegramAlert,
    result_wait: float,
) -> None:
    """Run a single monitoring cycle: check all IPs, evaluate, alert."""

    # Select nodes once for the whole cycle
    selected_nodes = node_manager.select_nodes()
    if not selected_nodes:
        logger.error("No nodes available — skipping cycle")
        return

    for ip in ips:
        try:
            logger.info("Starting ping check for %s", ip)
            response = api.start_ping_check(ip, nodes=selected_nodes)

            if not response.get("ok"):
                logger.error("check-ping failed for %s: %s", ip, response)
                continue

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

            # Alert based on condition
            if ev.condition == Condition.DOWN:
                telegram.alert_down(ip, ev.total_nodes, permanent_link)
            elif ev.condition == Condition.IRAN_ONLY:
                telegram.alert_iran_only(
                    ip,
                    ev.iran_ok,
                    ev.global_failed,
                    permanent_link,
                )
            elif ev.condition == Condition.DEGRADED:
                telegram.alert_degraded(
                    ip,
                    len(ev.ok_nodes),
                    ev.total_nodes,
                    ev.ok_pct,
                    permanent_link,
                )
            # Condition.OK → no alert

        except CheckHostError as exc:
            logger.error("API error for %s: %s", ip, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error checking %s: %s", ip, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="IP Access Monitor")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    args = parser.parse_args()

    config = load_config()
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
        chat_id=config["telegram_chat_id"],
    )

    logger.info(
        "Monitor started — %d IP(s), interval=%ds, threshold=%.0f%%",
        len(ips),
        config["check_interval"],
        config["alert_threshold"] * 100,
    )

    if args.once:
        run_cycle(ips, api, node_manager, evaluator, telegram, config["result_wait"])
        return

    while True:
        cycle_start = time.time()
        try:
            run_cycle(ips, api, node_manager, evaluator, telegram, config["result_wait"])
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            break
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle failed: %s", exc)

        elapsed = time.time() - cycle_start
        sleep_time = max(0, config["check_interval"] - elapsed)
        logger.info("Cycle complete (%.1fs) — sleeping %.0fs", elapsed, sleep_time)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
