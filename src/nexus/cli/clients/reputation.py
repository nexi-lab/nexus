"""Reputation and dispute HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class ReputationClient(BaseServiceClient):
    """Client for reputation, feedback, and dispute endpoints."""

    def show(
        self,
        agent_id: str,
        *,
        context: str = "general",
        window: str = "all_time",
    ) -> dict[str, Any]:
        """Get agent reputation profile."""
        return self._request(
            "GET",
            f"/api/v2/agents/{agent_id}/reputation",
            params={"context": context, "window": window},
        )

    def trust_score(self, agent_id: str) -> dict[str, Any]:
        """Get agent trust score."""
        return self._request("GET", f"/api/v2/agents/{agent_id}/trust-score")

    def leaderboard(
        self,
        *,
        zone_id: str | None = None,
        context: str = "general",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get reputation leaderboard."""
        return self._request(
            "GET",
            "/api/v2/reputation/leaderboard",
            params={"zone_id": zone_id, "context": context, "limit": limit},
        )

    def submit_feedback(
        self,
        exchange_id: str,
        *,
        outcome: str,
        reliability_score: float | None = None,
        quality_score: float | None = None,
        memo: str | None = None,
    ) -> dict[str, Any]:
        """Submit feedback for an exchange."""
        body: dict[str, Any] = {"outcome": outcome}
        if reliability_score is not None:
            body["reliability_score"] = reliability_score
        if quality_score is not None:
            body["quality_score"] = quality_score
        if memo:
            body["memo"] = memo
        return self._request(
            "POST",
            f"/api/v2/exchanges/{exchange_id}/feedback",
            json_body=body,
        )

    def get_feedback(self, exchange_id: str) -> dict[str, Any]:
        """Get feedback for an exchange."""
        return self._request("GET", f"/api/v2/exchanges/{exchange_id}/feedback")

    def dispute_create(self, exchange_id: str, *, reason: str) -> dict[str, Any]:
        """File a dispute for an exchange."""
        return self._request(
            "POST",
            f"/api/v2/exchanges/{exchange_id}/dispute",
            json_body={"reason": reason},
        )

    def dispute_get(self, dispute_id: str) -> dict[str, Any]:
        """Get dispute details."""
        return self._request("GET", f"/api/v2/disputes/{dispute_id}")

    def dispute_resolve(self, dispute_id: str, *, resolution: str) -> dict[str, Any]:
        """Resolve a dispute."""
        return self._request(
            "POST",
            f"/api/v2/disputes/{dispute_id}/resolve",
            json_body={"resolution": resolution},
        )
