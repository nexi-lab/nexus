"""Mounts /.activity/ at startup and ensures each agent has read access to
its own JSONL log.

Constructor takes function references rather than importing mount/ReBAC
services directly — keeps the brick testable and avoids cross-brick imports.
The owning lifespan wires the real implementations.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol


class _AddMount(Protocol):
    async def __call__(self, *, path: str, backend: Any) -> None: ...


class _AddReBACGrant(Protocol):
    def __call__(self, *, subject: str, relation: str, object: str) -> None: ...


_MOUNT_PATH = "/.activity/"


def _grant_for(agent_id: str) -> tuple[str, str, str]:
    return (f"agent:{agent_id}", "can-read", f"path:/.activity/*/{agent_id}.jsonl")


class AgentLogBrick:
    def __init__(
        self,
        *,
        add_mount: _AddMount,
        add_rebac_grant: _AddReBACGrant,
        store: Any | None,
    ) -> None:
        self._add_mount = add_mount
        self._add_grant = add_rebac_grant
        self._store = store

    async def startup(self, *, agent_ids: Iterable[str]) -> None:
        # The store is owned by the activity service. The brick may be
        # constructed with `store=None` if the activity service is disabled,
        # in which case mount registration is skipped.
        if self._store is not None:
            await self._add_mount(path=_MOUNT_PATH, backend=self._store)
        for agent_id in agent_ids:
            self._grant(agent_id)

    def on_agent_created(self, agent_id: str) -> None:
        self._grant(agent_id)

    def _grant(self, agent_id: str) -> None:
        subject, relation, obj = _grant_for(agent_id)
        self._add_grant(subject=subject, relation=relation, object=obj)
