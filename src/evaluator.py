"""Evaluate ping results and determine the alert condition.

Result categories:
  - DOWN      → 0 nodes have OK ping
  - IRAN_ONLY → only Iranian nodes have OK ping, all others (incl. Germany) fail
  - DEGRADED  → some nodes OK but below threshold
  - OK        → ≥ threshold fraction of nodes have OK ping
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .nodes import NodeManager

logger = logging.getLogger(__name__)

IRAN_CODE = "ir"


class Condition(Enum):
    DOWN = "down"
    IRAN_ONLY = "iran_only"
    DEGRADED = "degraded"
    OK = "ok"


@dataclass
class EvaluationResult:
    condition: Condition
    ip: str
    total_nodes: int
    ok_nodes: list[str]
    failed_nodes: list[str]
    permanent_link: str
    iran_ok: list[str]
    iran_failed: list[str]
    global_ok: list[str]
    global_failed: list[str]
    ok_pct: float


class ResultEvaluator:
    """Evaluates check-host.net ping results against alert conditions."""

    def __init__(
        self,
        node_manager: NodeManager,
        threshold: float = 0.7,
        priority_countries: set[str] | None = None,
    ) -> None:
        self.node_manager = node_manager
        self.threshold = threshold
        self.priority_countries = priority_countries or {"ir", "de"}
        self._country_cache: dict[str, str] = {}

    def evaluate(
        self,
        ip: str,
        results: dict[str, Any],
        permanent_link: str,
    ) -> EvaluationResult:
        """Evaluate raw ping results for a single IP."""
        ok_nodes: list[str] = []
        failed_nodes: list[str] = []

        for node_id, raw in results.items():
            if self._is_ping_ok(raw):
                ok_nodes.append(node_id)
            else:
                failed_nodes.append(node_id)

        # Split by Iran vs non-Iran (Germany is non-Iran for condition logic)
        iran_ok: list[str] = []
        iran_failed: list[str] = []
        global_ok: list[str] = []
        global_failed: list[str] = []

        for node_id in ok_nodes + failed_nodes:
            country = self._node_country(node_id)
            is_ok = node_id in ok_nodes
            if country == IRAN_CODE:
                iran_ok.append(node_id) if is_ok else iran_failed.append(node_id)
            else:
                global_ok.append(node_id) if is_ok else global_failed.append(node_id)

        total = len(results)
        ok_count = len(ok_nodes)
        pct = (ok_count / total * 100) if total else 0.0

        # Determine condition
        if ok_count == 0:
            condition = Condition.DOWN
        elif iran_ok and not global_ok:
            # Only Iran nodes can ping — everything else (Germany, global) fails
            condition = Condition.IRAN_ONLY
        elif ok_count / total >= self.threshold:
            condition = Condition.OK
        else:
            condition = Condition.DEGRADED

        logger.info(
            "Evaluated %s → %s (ok=%d/%d, %.0f%%, iran_ok=%d, global_ok=%d)",
            ip,
            condition.value,
            ok_count,
            total,
            pct,
            len(iran_ok),
            len(global_ok),
        )

        return EvaluationResult(
            condition=condition,
            ip=ip,
            total_nodes=total,
            ok_nodes=ok_nodes,
            failed_nodes=failed_nodes,
            permanent_link=permanent_link,
            iran_ok=iran_ok,
            iran_failed=iran_failed,
            global_ok=global_ok,
            global_failed=global_failed,
            ok_pct=pct,
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_ping_ok(raw: Any) -> bool:
        """Determine whether a node's ping result indicates success.

        Ping result formats from check-host.net:
          - ``null``                → still checking (treat as not OK)
          - ``[[null]]``            → DNS failure (not OK)
          - ``[[["OK", time, ip], ...]]`` → at least one "OK" in the list → OK
        """
        if raw is None:
            return False

        # The API wraps results in an outer list
        if isinstance(raw, list):
            for ping_set in raw:
                if ping_set is None:
                    continue
                if isinstance(ping_set, list):
                    for ping in ping_set:
                        if isinstance(ping, list) and ping and ping[0] == "OK":
                            return True
        return False

    def _node_country(self, node_id: str) -> str:
        if node_id in self._country_cache:
            return self._country_cache[node_id]
        info = self.node_manager.get_node_info(node_id)
        country = NodeManager._country_code(info) if info else ""
        self._country_cache[node_id] = country
        return country
