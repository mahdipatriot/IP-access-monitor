"""Node selection and caching for check-host.net.

Fetches the full node list from the API, caches it locally, and selects
a set of nodes that always includes **all** nodes from priority countries
(Iran and Germany by default) plus a spread of other global nodes.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from .checkhost_api import CheckHostAPI

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "node_cache.json"
DEFAULT_PRIORITY_COUNTRIES = {"ir", "de"}


class NodeManager:
    """Manages the check-host.net node list: fetch, cache, select."""

    def __init__(
        self,
        api: CheckHostAPI,
        cache_path: Path | None = None,
        cache_ttl: int = 86_400,
        max_global_nodes: int = 20,
        priority_countries: set[str] | None = None,
    ) -> None:
        self.api = api
        self.cache_path = cache_path or DEFAULT_CACHE_PATH
        self.cache_ttl = cache_ttl
        self.max_global_nodes = max_global_nodes
        self.priority_countries = priority_countries or DEFAULT_PRIORITY_COUNTRIES

    # ------------------------------------------------------------------ #
    #  Cache management                                                   #
    # ------------------------------------------------------------------ #

    def _load_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            age = time.time() - data.get("fetched_at", 0)
            if age < self.cache_ttl:
                logger.info("Loaded node cache (%d nodes, %.0fh old)", len(data.get("nodes", {})), age / 3600)
                return data
            logger.info("Node cache expired (age %.0fh > TTL %.0fh)", age / 3600, self.cache_ttl / 3600)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to read node cache: %s", exc)
        return None

    def _save_cache(self, nodes: dict[str, Any]) -> None:
        data = {"fetched_at": time.time(), "nodes": nodes}
        try:
            self.cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Saved node cache (%d nodes)", len(nodes))
        except OSError as exc:
            logger.warning("Failed to write node cache: %s", exc)

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def get_nodes(self) -> dict[str, dict[str, Any]]:
        """Return the full node list, fetching from API if cache is stale."""
        cached = self._load_cache()
        if cached is not None:
            return cached["nodes"]

        logger.info("Fetching fresh node list from check-host.net")
        raw = self.api.get_nodes()
        nodes = raw.get("nodes", {})
        self._save_cache(nodes)
        return nodes

    def select_nodes(self) -> list[str]:
        """Select nodes for checks.

        Strategy:
          1. Include **all** nodes from priority countries (ir, de).
          2. Fill remaining slots with a spread of other global nodes
             (max ``max_global_nodes`` non-priority nodes).
        """
        all_nodes = self.get_nodes()
        priority: list[str] = []
        global_nodes: list[str] = []

        for node_id, info in all_nodes.items():
            country = self._country_code(info)
            if country in self.priority_countries:
                priority.append(node_id)
            else:
                global_nodes.append(node_id)

        # Sort global nodes for deterministic selection, then take a spread
        global_nodes.sort()
        selected_global = global_nodes[: self.max_global_nodes]

        selected = priority + selected_global
        logger.info(
            "Selected %d nodes: %d priority (%s) + %d global",
            len(selected),
            len(priority),
            ", ".join(sorted(self.priority_countries)),
            len(selected_global),
        )
        return selected

    def get_node_info(self, node_id: str) -> dict[str, Any] | None:
        """Return metadata for a single node, or ``None`` if unknown."""
        nodes = self.get_nodes()
        return nodes.get(node_id)

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _country_code(node_info: dict[str, Any]) -> str:
        """Extract the lowercase country code from a node entry."""
        location = node_info.get("location", [])
        if isinstance(location, list) and location:
            return str(location[0]).lower()
        return ""
