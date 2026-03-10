"""RLM (Recursive Language Model) HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class RLMClient(BaseServiceClient):
    """Client for RLM inference endpoints."""

    def infer(
        self,
        path: str,
        *,
        prompt: str,
        model: str | None = None,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        """Run RLM inference on a context path."""
        body: dict[str, Any] = {"context_paths": [path], "query": prompt}
        if model:
            body["model"] = model
        if max_iterations is not None:
            body["max_iterations"] = max_iterations
        return self._request("POST", "/api/v2/rlm/infer", json_body=body)

    def status(self) -> dict[str, Any]:
        """Get RLM service status."""
        return self._request("GET", "/api/v2/rlm/status")
