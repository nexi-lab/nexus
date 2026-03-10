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
        rater_agent_id: str,
        rated_agent_id: str,
        outcome: str,
        reliability_score: float | None = None,
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        """Submit feedback for an exchange."""
        body: dict[str, Any] = {
            "rater_agent_id": rater_agent_id,
            "rated_agent_id": rated_agent_id,
            "outcome": outcome,
        }
        if reliability_score is not None:
            body["reliability_score"] = reliability_score
        if quality_score is not None:
            body["quality_score"] = quality_score
        return self._request(
            "POST",
            f"/api/v2/exchanges/{exchange_id}/feedback",
            json_body=body,
        )

    def get_feedback(self, exchange_id: str) -> dict[str, Any]:
        """Get feedback for an exchange."""
        return self._request("GET", f"/api/v2/exchanges/{exchange_id}/feedback")

    def dispute_create(
        self,
        exchange_id: str,
        *,
        complainant_agent_id: str,
        respondent_agent_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """File a dispute for an exchange."""
        return self._request(
            "POST",
            f"/api/v2/exchanges/{exchange_id}/dispute",
            json_body={
                "complainant_agent_id": complainant_agent_id,
                "respondent_agent_id": respondent_agent_id,
                "reason": reason,
            },
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
