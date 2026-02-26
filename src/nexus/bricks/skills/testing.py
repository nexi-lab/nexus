"""In-memory test fakes for the Skills brick (Issue #2035).

Provides protocol-compatible fakes that allow SkillService and
SkillPackageService to be tested in complete isolation from NexusFS,
ReBAC, and all other kernel dependencies.

Usage:
    from nexus.bricks.skills.testing import (
        FakeOperationContext,
        InMemorySkillFilesystem,
        StubSkillPermissions,
    )
    from nexus.bricks.skills.service import SkillService

    fs = InMemorySkillFilesystem()
    perms = StubSkillPermissions()
    ctx = FakeOperationContext(user_id="alice", zone_id="acme")
    svc = SkillService(fs=fs, perms=perms)

    svc.subscribe("/zone/acme/user/alice/skill/test/", ctx)
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeOperationContext:
    """Minimal context that satisfies SkillOperationContext protocol."""

    user_id: str = "test-user"
    zone_id: str = "test-zone"
    groups: list[str] = field(default_factory=list)
    is_system: bool = False
    is_admin: bool = False
    agent_id: str | None = None
    subject_id: str | None = None
    subject_type: str = "user"


class InMemorySkillFilesystem:
    """In-memory implementation of SkillFilesystemProtocol for testing.

    Stores files as a flat dict mapping paths to bytes/str content.
    Directories are implicit (any prefix of an existing path is a directory).
    """

    def __init__(self) -> None:
        self._files: dict[str, bytes | str] = {}

    def sys_read(self, path: str, *, context: Any = None) -> bytes | str:
        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        return self._files[path]

    def sys_write(self, path: str, content: bytes | str, *, context: Any = None) -> None:
        self._files[path] = content

    def sys_mkdir(self, path: str, *, context: Any = None) -> None:
        # Directories are implicit — no-op
        pass

    def sys_readdir(self, path: str, *, context: Any = None) -> list[str]:
        prefix = path if path.endswith("/") else path + "/"
        return [p for p in sorted(self._files) if p.startswith(prefix)]

    def sys_access(self, path: str, *, context: Any = None) -> bool:
        if path in self._files:
            return True
        # Check if any file exists under this path (directory check)
        prefix = path if path.endswith("/") else path + "/"
        return any(p.startswith(prefix) for p in self._files)

    def seed_skill(
        self,
        path: str,
        name: str = "test-skill",
        description: str = "A test skill",
        content: str = "# Test",
    ) -> None:
        """Helper to seed a skill at the given path."""
        if not path.endswith("/"):
            path += "/"
        skill_md = (
            f"---\nname: {name}\ndescription: {description}\n"
            f"version: '1.0'\ntags:\n  - test\n---\n{content}"
        )
        self._files[f"{path}SKILL.md"] = skill_md.encode("utf-8")


class StubSkillPermissions:
    """Stub implementation of SkillPermissionProtocol for testing.

    By default, all permission checks return True (permissive mode).
    Override behavior by setting check_result, or by adding specific
    tuples to the tuples list.
    """

    def __init__(self) -> None:
        self.check_result: bool = True
        self.tuples: list[dict[str, Any]] = []
        self.created_tuples: list[dict[str, Any]] = []
        self._rebac_manager = _StubReBACManager(self)
        self._invalidated_paths: list[str] = []

    def rebac_check(
        self,
        *,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        return self.check_result

    def rebac_create(
        self,
        *,
        subject: tuple[str, ...],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any] | None:
        tuple_entry = {
            "tuple_id": f"tuple-{len(self.created_tuples) + 1}",
            "subject_type": subject[0],
            "subject_id": subject[1],
            "relation": relation,
            "object_type": object[0],
            "object_id": object[1],
        }
        self.created_tuples.append(tuple_entry)
        return {"tuple_id": tuple_entry["tuple_id"]}

    def rebac_list_tuples(
        self,
        *,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        result = []
        for t in self.tuples:
            if subject and (
                t.get("subject_type") != subject[0] or t.get("subject_id") != subject[1]
            ):
                continue
            if relation and t.get("relation") != relation:
                continue
            if object and (t.get("object_type") != object[0] or t.get("object_id") != object[1]):
                continue
            result.append(t)
        return result

    def rebac_delete_object_tuples(
        self,
        *,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> int:
        before = len(self.tuples)
        self.tuples = [
            t
            for t in self.tuples
            if not (t.get("object_type") == object[0] and t.get("object_id") == object[1])
        ]
        return before - len(self.tuples)

    def invalidate_metadata_cache(self, *paths: str) -> None:
        self._invalidated_paths.extend(paths)

    @property
    def rebac_manager(self) -> Any:
        return self._rebac_manager


class _StubReBACManager:
    """Minimal ReBAC manager stub for testing."""

    def __init__(self, perms: StubSkillPermissions):
        self._perms = perms

    def rebac_check(self, **kwargs: Any) -> bool:
        return self._perms.check_result

    def rebac_delete(self, tuple_id: str) -> None:
        self._perms.tuples = [t for t in self._perms.tuples if t.get("tuple_id") != tuple_id]
