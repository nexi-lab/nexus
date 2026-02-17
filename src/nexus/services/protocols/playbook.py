"""Playbook protocol (ops-scenario-matrix S22: ACE).

Defines the contract for playbook management — creating, updating,
querying, deleting playbooks, recording usage, and retrieving
relevant strategies.

Maps 1:1 to ``services/ace/playbook.PlaybookManager``.

Storage Affinity: **RecordStore** (playbook records, strategy entries).

References:
    - docs/architecture/ops-scenario-matrix.md  (S22)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #549: ISP split of TrajectoryProtocol
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


@runtime_checkable
class PlaybookProtocol(Protocol):
    """Service contract for playbook management (PlaybookManager).

    Covers CRUD for playbooks, recording usage outcomes,
    and retrieving strategies relevant to a task.
    """

    def create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: Literal["agent", "user", "zone", "global"] = "agent",
        visibility: Literal["private", "shared", "public"] = "private",
        initial_strategies: list[dict[str, Any]] | None = None,
    ) -> str: ...

    def get_playbook(
        self,
        playbook_id: str,
    ) -> dict[str, Any] | None: ...

    def update_playbook(
        self,
        playbook_id: str,
        strategies: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        increment_version: bool = True,
    ) -> None: ...

    def record_usage(
        self,
        playbook_id: str,
        success: bool,
        improvement_score: float | None = None,
    ) -> None: ...

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        name_pattern: str | None = None,
        limit: int = 50,
        path: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def delete_playbook(
        self,
        playbook_id: str,
    ) -> bool: ...

    def get_relevant_strategies(
        self,
        playbook_id: str,
        task_description: str,
        strategy_type: Literal["helpful", "harmful", "neutral"] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...
