"""Scheduler HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class SchedulerClient(BaseServiceClient):
    """Client for scheduler and task management endpoints."""

    def status(self) -> dict[str, Any]:
        """Get scheduler metrics."""
        return self._request("GET", "/api/v2/scheduler/metrics")

    def task_status(self, task_id: str) -> dict[str, Any]:
        """Get status of a specific task."""
        return self._request("GET", f"/api/v2/scheduler/task/{task_id}")

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel a running task."""
        return self._request("POST", f"/api/v2/scheduler/task/{task_id}/cancel")
