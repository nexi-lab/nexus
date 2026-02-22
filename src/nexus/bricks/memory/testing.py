"""Testing fakes for the Memory brick (Issue #2177).

Provides protocol-compatible in-memory implementations for unit testing
without requiring a real database or ReBAC manager.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeOperationContext:
    """Minimal OperationContext equivalent for testing."""

    user_id: str = "test-user"
    zone_id: str | None = "test-zone"
    agent_id: str | None = None
    groups: list[str] = field(default_factory=list)
    is_admin: bool = False
    is_system: bool = False
    subject_type: str = "user"
    subject_id: str | None = None

    def get_subject(self) -> tuple[str, str]:
        return (self.subject_type, self.subject_id or self.user_id)


class StubPermissionEnforcer:
    """Always-pass permission enforcer for testing.

    Satisfies MemoryPermissionProtocol.
    """

    def __init__(self, *, check_result: bool = True) -> None:
        self.check_result = check_result
        self._created_tuples: list[dict[str, Any]] = []

    def check_memory(self, memory: Any, permission: Any, context: Any) -> bool:  # noqa: ARG002
        return self.check_result

    def create_entity_tuples(
        self,
        memory_id: str,
        zone_id: str | None,
        user_id: str | None,
        agent_id: str | None,
    ) -> None:
        self._created_tuples.append(
            {
                "memory_id": memory_id,
                "zone_id": zone_id,
                "user_id": user_id,
                "agent_id": agent_id,
            }
        )


class InMemoryEntityRegistry:
    """Dict-backed entity registry for testing.

    Satisfies MemoryEntityRegistryProtocol.
    """

    def __init__(self) -> None:
        self._entities: dict[tuple[str, str], dict[str, Any]] = {}

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        parent_type: str | None = None,
        parent_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        key = (entity_type, entity_id)
        entity = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "parent_type": parent_type,
            "parent_id": parent_id,
            **kwargs,
        }
        self._entities[key] = entity
        return entity

    def extract_ids_from_path_parts(self, parts: list[str]) -> dict[str, str]:
        ids: dict[str, str] = {}
        for part in parts:
            for etype, eid in self._entities:
                if eid == part:
                    ids[f"{etype}_id"] = eid
        return ids

    def lookup_entity_by_id(self, entity_id: str) -> list[Any]:
        results = []
        for (_etype, eid), entity in self._entities.items():
            if eid == entity_id:
                results.append(type("Entity", (), entity)())
        return results
