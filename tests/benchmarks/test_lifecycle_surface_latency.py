"""Lifecycle surface latency guardrails for issue #4137.

These tests are intentionally lightweight and run without pytest-benchmark.
They time representative service-layer hot/control paths so the surface map can
link benchmark evidence for agents, workspaces, snapshots, and versions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.bricks.workspace.workspace_registry import WorkspaceConfig
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.process_types import AgentState
from nexus.contracts.types import OperationContext
from nexus.server.rpc.services.snapshots_rpc import SnapshotsRPCService
from nexus.services.agents.agent_rpc_service import AgentRPCService
from nexus.services.workspace.workspace_rpc_service import WorkspaceRPCService
from nexus.storage.models import Base, FilePathModel, VersionHistoryModel
from nexus.storage.version_manager import VersionManager


def _elapsed_ms(fn, *args, **kwargs) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start) * 1000


class _AgentRegistry:
    def __init__(self) -> None:
        self.count = 0

    def heartbeat(self, _agent_id: str) -> None:
        self.count += 1


def test_agent_heartbeat_under_1ms() -> None:
    registry = _AgentRegistry()
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=MagicMock(),
        session_factory=MagicMock(),
        agent_registry=registry,
    )

    _result, elapsed_ms = _elapsed_ms(service.agent_heartbeat, "alice")

    assert registry.count == 1
    assert elapsed_ms < 1.0


@dataclass
class _AgentRecord:
    pid: str
    owner_id: str = "alice"
    zone_id: str = ROOT_ZONE_ID
    name: str = "agent"
    state: AgentState = AgentState.READY
    generation: int = 1
    created_at_ms: int = 1_700_000_000_000
    updated_at_ms: int = 1_700_000_001_000
    external_info: dict[str, int] | None = None


class _AgentListRegistry:
    def __init__(self, count: int) -> None:
        self.records = [
            _AgentRecord(
                pid=f"agent-{i}",
                name=f"Agent {i}",
                external_info={"last_heartbeat_ms": 1_700_000_002_000},
            )
            for i in range(count)
        ]

    def list_processes(self, *, zone_id: str, state: str | None = None) -> list[_AgentRecord]:
        return [
            r
            for r in self.records
            if r.zone_id == zone_id and (state is None or r.state.value == state)
        ]


def test_agent_list_by_zone_1000_under_25ms() -> None:
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=MagicMock(),
        session_factory=MagicMock(),
        agent_registry=_AgentListRegistry(1000),
    )

    result, elapsed_ms = _elapsed_ms(service.agent_list_by_zone, ROOT_ZONE_ID)

    assert len(result) == 1000
    assert elapsed_ms < 25.0


class _WorkspaceRegistry:
    def __init__(self, workspaces: list[WorkspaceConfig]) -> None:
        self._workspaces = workspaces

    def list_workspaces(self) -> list[WorkspaceConfig]:
        return self._workspaces


def test_workspace_list_1000_entries_under_25ms() -> None:
    workspaces = [
        WorkspaceConfig(
            path=f"/zone/{ROOT_ZONE_ID}/user/alice/workspace/project-{i}",
            name=f"project-{i}",
            created_at=datetime.now(UTC),
            created_by="alice",
        )
        for i in range(1000)
    ]
    service = WorkspaceRPCService(
        workspace_manager=MagicMock(),
        workspace_registry=_WorkspaceRegistry(workspaces),
        vfs=MagicMock(),
        default_context=OperationContext(user_id="alice", groups=[], zone_id=ROOT_ZONE_ID),
    )

    result, elapsed_ms = _elapsed_ms(
        service.list_workspaces,
        context=OperationContext(user_id="alice", groups=[], zone_id=ROOT_ZONE_ID),
    )

    assert len(result) == 1000
    assert elapsed_ms < 25.0


@dataclass
class _Entry:
    path: str
    operation: str = "write"
    content_id: str = "abc123"


class _SnapshotService:
    async def list_entries(self, _transaction_id: str) -> list[_Entry]:
        return [_Entry(path=f"/workspace/file-{i}.txt") for i in range(1000)]


@pytest.mark.asyncio()
async def test_snapshot_list_entries_1000_under_200ms() -> None:
    service = SnapshotsRPCService(_SnapshotService())
    await service.snapshot_list_entries("warmup")

    start = time.perf_counter()
    result = await service.snapshot_list_entries("txn-1")
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result["count"] == 1000
    assert elapsed_ms < 200.0


def test_version_list_and_diff_200_versions_under_25ms() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    path_id = "path-1"
    path = "/workspace/versioned.txt"

    with Session() as session:
        session.add(
            FilePathModel(
                path_id=path_id,
                zone_id=ROOT_ZONE_ID,
                virtual_path=path,
                size_bytes=200,
                content_id="content-200",
                current_version=200,
            )
        )
        session.add_all(
            VersionHistoryModel(
                resource_type="file",
                resource_id=path_id,
                version_number=i,
                content_id=f"content-{i}",
                size_bytes=i,
                mime_type="text/plain",
                created_by="alice",
            )
            for i in range(1, 201)
        )
        session.commit()

        versions, list_ms = _elapsed_ms(VersionManager.list_versions, session, path)
        diff, diff_ms = _elapsed_ms(VersionManager.get_version_diff, session, path, 1, 200)

    assert len(versions) == 200
    assert diff["content_changed"] is True
    assert list_ms < 25.0
    assert diff_ms < 25.0
