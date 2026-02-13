"""Shared fixtures for ACE router unit tests.

Provides common mock services and test app configurations
used across memory, trajectory, feedback, playbook,
reflect, curate, and consolidation router tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_operation_context():
    """Auto-patch _get_operation_context so routers never hit real fastapi_server.

    We patch both the canonical location and any module that has already
    imported the function via ``from ... import _get_operation_context``.
    """
    mock_ctx = MagicMock()
    mock_ctx.user_id = "test-agent"
    mock_ctx.user = "test-agent"
    mock_ctx.zone_id = "default"
    mock_ctx.agent_id = None

    with (
        patch(
            "nexus.server.api.v2.dependencies._get_operation_context",
            return_value=mock_ctx,
        ),
        patch(
            "nexus.server.api.v2.routers.memories._get_operation_context",
            return_value=mock_ctx,
        ),
    ):
        yield mock_ctx


@pytest.fixture
def mock_auth_result():
    """Mock auth result dict matching require_auth output."""
    return {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "test-agent",
        "zone_id": "default",
        "is_admin": False,
        "x_agent_id": None,
        "metadata": {},
    }


@pytest.fixture
def mock_nexus_fs():
    """Mock NexusFS with memory API and sub-components."""
    nexus_fs = MagicMock()

    # Memory API mock
    memory = MagicMock()
    memory.session = MagicMock()
    memory.backend = MagicMock()
    memory.store.return_value = "mem-123"
    memory.get.return_value = {
        "memory_id": "mem-123",
        "content": "test content",
        "scope": "user",
        "state": "active",
        "memory_type": "fact",
        "importance": 0.5,
        "namespace": None,
        "path_key": "pk-123",
    }
    memory.delete.return_value = True
    memory.invalidate.return_value = True
    memory.revalidate.return_value = True
    memory.search.return_value = [{"memory_id": "mem-1", "content": "result"}]
    memory.query.return_value = [{"memory_id": "mem-1", "content": "result"}]
    memory.list_versions.return_value = [
        {"version": 1, "content_hash": "abc", "created_at": "2025-01-01T00:00:00"}
    ]
    memory.get_version.return_value = {"version": 1, "content": "old content"}
    memory.rollback.return_value = None
    memory.diff_versions.return_value = {"v1": 1, "v2": 2, "diff": "changes"}
    memory.get_history.return_value = [
        {"memory_id": "mem-old", "content": "old"},
        {"memory_id": "mem-123", "content": "new"},
    ]
    memory.apply_decay_batch.return_value = {
        "success": True,
        "updated": 10,
        "skipped": 2,
        "processed": 12,
    }
    memory.ensure_upsert_key.return_value = "pk-123"

    # resolve_to_current mock
    current_model = MagicMock()
    current_model.memory_id = "mem-123"
    current_model.current_version = 2
    memory.resolve_to_current.return_value = current_model

    # memory_router sub-mock for rollback
    memory_router = MagicMock()
    rollback_model = MagicMock()
    rollback_model.current_version = 1
    memory_router.get_memory_by_id.return_value = rollback_model
    memory.memory_router = memory_router

    nexus_fs.memory = memory
    nexus_fs._llm_provider = MagicMock()

    return nexus_fs


@pytest.fixture
def mock_memory_api(mock_nexus_fs):
    """Shortcut to mock_nexus_fs.memory."""
    return mock_nexus_fs.memory


@pytest.fixture
def mock_trajectory_manager():
    """Mock TrajectoryManager with sensible defaults."""
    manager = MagicMock()
    manager.start_trajectory.return_value = "traj-123"
    manager.log_step.return_value = None
    manager.complete_trajectory.return_value = "traj-123"
    manager.query_trajectories.return_value = [
        {"trajectory_id": "traj-1", "task_description": "test", "status": "in_progress"}
    ]
    manager.get_trajectory.return_value = {
        "trajectory_id": "traj-123",
        "task_description": "test task",
        "status": "in_progress",
        "trace": [],
    }
    return manager


@pytest.fixture
def mock_feedback_manager():
    """Mock FeedbackManager with sensible defaults."""
    manager = MagicMock()
    manager.add_feedback.return_value = "fb-123"
    manager.get_trajectory_feedback.return_value = [
        {"feedback_id": "fb-1", "score": 0.8, "feedback_type": "human"}
    ]
    manager.get_effective_score.return_value = 0.85
    manager.get_relearning_queue.return_value = [
        {"trajectory_id": "traj-1", "reason": "low score", "priority": 5}
    ]
    manager.mark_for_relearning.return_value = None
    return manager


@pytest.fixture
def mock_playbook_manager():
    """Mock PlaybookManager with sensible defaults."""
    manager = MagicMock()
    manager.create_playbook.return_value = "pb-123"
    manager.get_playbook.return_value = {
        "playbook_id": "pb-123",
        "name": "test playbook",
        "scope": "agent",
        "visibility": "private",
        "strategies": [],
    }
    manager.query_playbooks.return_value = [
        {"playbook_id": "pb-1", "name": "test", "scope": "agent"}
    ]
    manager.update_playbook.return_value = None
    manager.delete_playbook.return_value = True
    manager.record_usage.return_value = None
    return manager


@pytest.fixture
def mock_reflector():
    """Mock Reflector with sensible defaults."""
    reflector = AsyncMock()
    reflector.reflect_async.return_value = {
        "memory_id": "mem-ref-123",
        "trajectory_id": "traj-123",
        "helpful_strategies": [{"strategy": "plan first"}],
        "harmful_patterns": [{"pattern": "rush coding"}],
        "observations": [{"observation": "good test coverage"}],
        "confidence": 0.85,
    }
    return reflector


@pytest.fixture
def mock_curator():
    """Mock Curator with sensible defaults."""
    curator = MagicMock()
    curator.curate_playbook.return_value = {
        "playbook_id": "pb-123",
        "strategies_added": 2,
        "strategies_merged": 1,
        "strategies_total": 5,
    }
    curator.curate_from_trajectory.return_value = {
        "strategies_added": 1,
        "strategies_merged": 0,
        "strategies_total": 3,
    }
    return curator


@pytest.fixture
def mock_consolidation_engine():
    """Mock ConsolidationEngine with sensible defaults."""
    engine = AsyncMock()
    engine.consolidate_by_affinity_async.return_value = {
        "clusters_formed": 1,
        "total_consolidated": 2,
        "archived_count": 2,
        "results": [
            {
                "consolidated_memory_id": "c1",
                "source_memory_ids": ["m1", "m2"],
                "memories_consolidated": 2,
            }
        ],
        "cluster_statistics": [],
    }
    return engine


@pytest.fixture
def mock_hierarchy_manager():
    """Mock HierarchicalMemoryManager with sensible defaults."""
    manager = AsyncMock()

    # Build hierarchy result
    result = MagicMock()
    result.total_memories = 10
    result.total_abstracts_created = 3
    result.max_level_reached = 2
    result.levels = {}
    result.statistics = {"avg_cluster_size": 3.3}
    manager.build_hierarchy_async.return_value = result

    # get_hierarchy_for_memory is called synchronously (no await), so use MagicMock
    manager.get_hierarchy_for_memory = MagicMock(
        return_value={
            "memory_id": "mem-123",
            "level": 0,
            "parent": None,
            "children": [],
        }
    )
    return manager


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider (non-None)."""
    return MagicMock()
