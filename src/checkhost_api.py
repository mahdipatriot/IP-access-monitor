"""check-host.net API client.

Handles all interactions with the check-host.net API:
  - Starting ping checks
  - Polling check results
  - Fetching the node list
"""

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://check-host.net"
HEADERS = {"Accept": "application/json"}
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
INITIAL_BACKOFF = 2  # seconds


class CheckHostError(Exception):
    """Raised when the check-host.net API returns an error."""


class CheckHostAPI:
    """Thin client around the check-host.net REST API."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    # ------------------------------------------------------------------ #
    #  Low-level request helper with exponential backoff                 #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        url: str,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        backoff = INITIAL_BACKOFF
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)

                # Rate-limited or server error → back off and retry
                if resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "API returned %d for %s (attempt %d/%d) — retrying in %ds",
                        resp.status_code,
                        url,
                        attempt,
                        MAX_RETRIES,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue

                resp.raise_for_status()
                return resp.json()

            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "Request failed for %s (attempt %d/%d): %s — retrying in %ds",
                    url,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2

        raise CheckHostError(f"Failed after {MAX_RETRIES} retries: {last_exc}")

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def start_ping_check(
        self,
        host: str,
        nodes: list[str] | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Start a ping check for *host*.

        Returns the raw JSON response containing ``request_id``, ``permanent_link``,
        and ``nodes``.
        """
        # check-host.net accepts multiple `node` params — use a list of tuples
        params: list[tuple[str, str]] = [("host", host)]
        if max_nodes is not None:
            params.append(("max_nodes", str(max_nodes)))
        if nodes:
            for n in nodes:
                params.append(("node", n))

        return self._request(f"{BASE_URL}/check-ping", params=params)

    def get_check_result(self, request_id: str) -> dict[str, Any]:
        """Poll the result of a check.  May return partial results (some nodes
        still ``null``) — the caller should re-poll if needed."""
        return self._request(f"{BASE_URL}/check-result/{request_id}")

    def get_nodes(self) -> dict[str, Any]:
        """Fetch the full list of check-host.net nodes."""
        return self._request(f"{BASE_URL}/nodes/hosts")

    def poll_until_complete(
        self,
        request_id: str,
        wait: float = 5.0,
        max_polls: int = 6,
    ) -> dict[str, Any]:
        """Poll ``get_check_result`` until all nodes have reported or *max_polls*
        is reached.

        A node is considered "reported" when its value is not ``None``.
        """
        result: dict[str, Any] = {}
        for poll in range(1, max_polls + 1):
            if poll > 1:
                time.sleep(wait)
            result = self.get_check_result(request_id)
            if self._all_nodes_reported(result):
                logger.debug("All nodes reported after %d poll(s)", poll)
                return result
            logger.debug(
                "Poll %d/%d — some nodes still pending for %s",
                poll,
                max_polls,
                request_id,
            )
        logger.warning("Reached max_polls (%d) for %s — returning partial results", max_polls, request_id)
        return result

    @staticmethod
    def _all_nodes_reported(result: dict[str, Any]) -> bool:
        """Return ``True`` when no node value is ``None`` (i.e. still pending)."""
        return all(value is not None for value in result.values())
