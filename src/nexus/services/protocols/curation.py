"""Curation protocol (ops-scenario-matrix S22: ACE).

Defines the contract for playbook curation — merging reflection
insights into playbooks and curating from individual trajectories.

Maps 1:1 to ``services/ace/curation.Curator``.

Storage Affinity: **RecordStore** (playbook strategy records).

References:
    - docs/architecture/ops-scenario-matrix.md  (S22)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #549: ISP split of TrajectoryProtocol
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CurationProtocol(Protocol):
    """Service contract for playbook curation (Curator).

    Covers merging reflection memories into a playbook and
    curating a playbook from a single trajectory.
    """

    def curate_playbook(
        self,
        playbook_id: str,
        reflection_memory_ids: list[str],
        merge_threshold: float = 0.7,
    ) -> dict[str, Any]: ...

    def curate_from_trajectory(
        self,
        playbook_id: str,
        trajectory_id: str,
    ) -> dict[str, Any] | None: ...
