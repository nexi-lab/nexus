"""Reflection protocol (ops-scenario-matrix S22: ACE).

Defines the contract for LLM-powered trajectory reflection —
analyzing a completed trajectory to extract lessons learned.

Maps 1:1 to ``services/ace/reflection.Reflector``.

Storage Affinity: **CacheStore** (LLM reflection cache) +
                  **RecordStore** (reflection memory records).

References:
    - docs/architecture/ops-scenario-matrix.md  (S22)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #549: ISP split of TrajectoryProtocol
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReflectionProtocol(Protocol):
    """Service contract for trajectory reflection (Reflector).

    Single async method that uses an LLM to analyze a completed
    trajectory and produce reflection insights.
    """

    async def reflect_async(
        self,
        trajectory_id: str,
        context: str | None = None,
        reflection_prompt: str | None = None,
    ) -> dict[str, Any]: ...
