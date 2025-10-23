"""LLM metrics tracking with Nexus metadata database integration."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenUsage:
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0  # Tokens read from cache (e.g., prompt cache hits)
    cache_write_tokens: int = 0  # Tokens written to cache

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Add two token usage objects."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class ResponseLatency:
    """Response latency information."""

    latency: float  # seconds
    response_id: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class LLMMetrics:
    """Metrics for LLM usage.

    Tracks costs, token usage, and latency for LLM API calls.
    Can be persisted to Nexus metadata database.
    """

    model_name: str
    accumulated_cost: float = 0.0
    accumulated_token_usage: TokenUsage = field(default_factory=TokenUsage)
    response_latencies: list[ResponseLatency] = field(default_factory=list)

    def add_cost(self, cost: float) -> None:
        """Add cost to accumulated total."""
        self.accumulated_cost += cost

    def add_token_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        response_id: str = "unknown",  # noqa: ARG002
    ) -> None:
        """Add token usage to accumulated total."""
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        self.accumulated_token_usage = self.accumulated_token_usage + usage

    def add_response_latency(self, latency: float, response_id: str) -> None:
        """Add response latency measurement."""
        self.response_latencies.append(ResponseLatency(latency=latency, response_id=response_id))

    def reset(self) -> None:
        """Reset all metrics."""
        self.accumulated_cost = 0.0
        self.accumulated_token_usage = TokenUsage()
        self.response_latencies.clear()

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary for storage."""
        return {
            "model_name": self.model_name,
            "accumulated_cost": self.accumulated_cost,
            "token_usage": {
                "prompt_tokens": self.accumulated_token_usage.prompt_tokens,
                "completion_tokens": self.accumulated_token_usage.completion_tokens,
                "cache_read_tokens": self.accumulated_token_usage.cache_read_tokens,
                "cache_write_tokens": self.accumulated_token_usage.cache_write_tokens,
                "total_tokens": self.accumulated_token_usage.total_tokens,
            },
            "response_latencies": [
                {"latency": rl.latency, "response_id": rl.response_id, "timestamp": rl.timestamp}
                for rl in self.response_latencies
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMMetrics:
        """Create metrics from dictionary."""
        token_usage_data = data.get("token_usage", {})
        token_usage = TokenUsage(
            prompt_tokens=token_usage_data.get("prompt_tokens", 0),
            completion_tokens=token_usage_data.get("completion_tokens", 0),
            cache_read_tokens=token_usage_data.get("cache_read_tokens", 0),
            cache_write_tokens=token_usage_data.get("cache_write_tokens", 0),
        )

        latencies_data = data.get("response_latencies", [])
        latencies = [
            ResponseLatency(
                latency=rl["latency"],
                response_id=rl["response_id"],
                timestamp=rl.get("timestamp", time.time()),
            )
            for rl in latencies_data
        ]

        return cls(
            model_name=data["model_name"],
            accumulated_cost=data.get("accumulated_cost", 0.0),
            accumulated_token_usage=token_usage,
            response_latencies=latencies,
        )

    @property
    def average_latency(self) -> float | None:
        """Calculate average response latency."""
        if not self.response_latencies:
            return None
        return sum(rl.latency for rl in self.response_latencies) / len(self.response_latencies)

    @property
    def total_requests(self) -> int:
        """Total number of requests made."""
        return len(self.response_latencies)


class MetricsStore:
    """Store for persisting LLM metrics to Nexus metadata database."""

    def __init__(self, metadata_path: str | None = None):
        """Initialize metrics store.

        Args:
            metadata_path: Path to Nexus metadata database
        """
        self.metadata_path = metadata_path
        # TODO: Integrate with Nexus metadata database when available

    def save_metrics(self, metrics: LLMMetrics, session_id: str | None = None) -> None:
        """Save metrics to database.

        Args:
            metrics: Metrics to save
            session_id: Optional session ID for grouping metrics
        """
        # TODO: Implement actual database storage
        # For now, this is a placeholder
        pass

    def load_metrics(self, session_id: str) -> LLMMetrics | None:  # noqa: ARG002
        """Load metrics from database.

        Args:
            session_id: Session ID to load metrics for

        Returns:
            Metrics if found, None otherwise
        """
        # TODO: Implement actual database retrieval
        return None

    def get_metrics_by_model(self, model_name: str) -> list[LLMMetrics]:  # noqa: ARG002
        """Get all metrics for a specific model.

        Args:
            model_name: Model name to filter by

        Returns:
            List of metrics for the model
        """
        # TODO: Implement actual database query
        return []
