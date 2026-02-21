"""Backward-compat shim identity tests.

Verifies that every shim module in ``nexus.services.*`` re-exports the
exact same object as the canonical module it redirects to.  This catches
stale shims, broken ``import *`` expansions, and missing explicit imports
for underscore-prefixed names.

Each parametrized case is a ``(old_module, new_module, name)`` triple.
The test asserts ``getattr(old_mod, name) is getattr(new_mod, name)``.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ─── Mapping: (old_module, new_module, exported_name) ────────────────────

_SHIM_CASES: list[tuple[str, str, str]] = [
    # ── system_services/sync/ ────────────────────────────────────────────
    (
        "nexus.services.sync_service",
        "nexus.system_services.sync.sync_service",
        "SyncService",
    ),
    (
        "nexus.services.sync_job_service",
        "nexus.system_services.sync.sync_job_service",
        "SyncJobService",
    ),
    (
        "nexus.services.sync_job_manager",
        "nexus.system_services.sync.sync_job_manager",
        "SyncJobManager",
    ),
    (
        "nexus.services.sync_backlog_store",
        "nexus.system_services.sync.sync_backlog_store",
        "SyncBacklogStore",
    ),
    (
        "nexus.services.change_log_store",
        "nexus.system_services.sync.change_log_store",
        "ChangeLogStore",
    ),
    (
        "nexus.services.write_back_service",
        "nexus.system_services.sync.write_back_service",
        "WriteBackService",
    ),
    (
        "nexus.services.write_back_metrics",
        "nexus.system_services.sync.write_back_metrics",
        "WriteBackMetrics",
    ),
    (
        "nexus.services.conflict_resolution",
        "nexus.system_services.sync.conflict_resolution",
        "ConflictStrategy",
    ),
    (
        "nexus.services.conflict_log_store",
        "nexus.system_services.sync.conflict_log_store",
        "ConflictLogStore",
    ),
    # ── system_services/lifecycle/ ───────────────────────────────────────
    (
        "nexus.services.brick_lifecycle",
        "nexus.system_services.lifecycle.brick_lifecycle",
        "BrickLifecycleManager",
    ),
    (
        "nexus.services.brick_reconciler",
        "nexus.system_services.lifecycle.brick_reconciler",
        "BrickReconciler",
    ),
    (
        "nexus.services.hook_engine",
        "nexus.system_services.lifecycle.hook_engine",
        "ScopedHookEngine",
    ),
    (
        "nexus.services.sessions",
        "nexus.system_services.lifecycle.sessions",
        "create_session",
    ),
    (
        "nexus.services.events_service",
        "nexus.system_services.lifecycle.events_service",
        "EventsService",
    ),
    (
        "nexus.services.reactive_subscriptions",
        "nexus.system_services.lifecycle.reactive_subscriptions",
        "ReactiveSubscriptionManager",
    ),
    (
        "nexus.services.workflow_dispatch_service",
        "nexus.system_services.lifecycle.workflow_dispatch_service",
        "WorkflowDispatchService",
    ),
    (
        "nexus.services.task_queue_service",
        "nexus.system_services.lifecycle.task_queue_service",
        "TaskQueueService",
    ),
    (
        "nexus.services.dedup_work_queue",
        "nexus.system_services.lifecycle.dedup_work_queue",
        "DedupWorkQueue",
    ),
    # ── system_services/workspace/ ───────────────────────────────────────
    (
        "nexus.services.workspace_manager",
        "nexus.system_services.workspace.workspace_manager",
        "WorkspaceManager",
    ),
    (
        "nexus.services.workspace_permissions",
        "nexus.system_services.workspace.workspace_permissions",
        "check_workspace_permission",
    ),
    (
        "nexus.services.context_branch",
        "nexus.system_services.workspace.context_branch",
        "ContextBranchService",
    ),
    (
        "nexus.services.overlay_resolver",
        "nexus.system_services.workspace.overlay_resolver",
        "OverlayResolver",
    ),
    # ── services/ domain subdirs: search ─────────────────────────────────
    (
        "nexus.services.search_service",
        "nexus.services.search.search_service",
        "SearchService",
    ),
    # search_semantic shim deleted (Issue #2075)
    (
        "nexus.services.graph_search_service",
        "nexus.services.search.graph_search_service",
        "DaemonSemanticSearchWrapper",
    ),
    # ── services/ domain subdirs: llm ────────────────────────────────────
    (
        "nexus.services.llm_service",
        "nexus.services.llm.llm_service",
        "LLMService",
    ),
    (
        "nexus.services.llm_document_reader",
        "nexus.services.llm.llm_document_reader",
        "LLMDocumentReader",
    ),
    (
        "nexus.services.llm_context_builder",
        "nexus.services.llm.llm_context_builder",
        "ContextBuilder",
    ),
    (
        "nexus.services.llm_citation",
        "nexus.services.llm.llm_citation",
        "DocumentReadResult",
    ),
    # ── services/ domain subdirs: mount ──────────────────────────────────
    (
        "nexus.services.mount_service",
        "nexus.services.mount.mount_service",
        "MountService",
    ),
    (
        "nexus.services.mount_core_service",
        "nexus.services.mount.mount_core_service",
        "MountCoreService",
    ),
    (
        "nexus.services.mount_manager",
        "nexus.services.mount.mount_manager",
        "MountManager",
    ),
    (
        "nexus.services.mount_persist_service",
        "nexus.services.mount.mount_persist_service",
        "MountPersistService",
    ),
    # ── services/ domain subdirs: oauth, mcp ─────────────────────────────
    (
        "nexus.services.oauth_service",
        "nexus.services.oauth.oauth_service",
        "OAuthService",
    ),
    (
        "nexus.services.mcp_service",
        "nexus.services.mcp.mcp_service",
        "MCPService",
    ),
    # ── services/ domain subdirs: upload ─────────────────────────────────
    (
        "nexus.services.chunked_upload_service",
        "nexus.services.upload.chunked_upload_service",
        "ChunkedUploadService",
    ),
    (
        "nexus.services.upload_session",
        "nexus.services.upload.upload_session",
        "UploadSession",
    ),
    # ── services/ domain subdirs: share_link ─────────────────────────────
    (
        "nexus.services.share_link_service",
        "nexus.services.share_link.share_link_service",
        "ShareLinkService",
    ),
    # ── services/ domain subdirs: versioning ─────────────────────────────
    (
        "nexus.services.version_service",
        "nexus.services.versioning.version_service",
        "VersionService",
    ),
    (
        "nexus.services.operation_undo_service",
        "nexus.services.versioning.operation_undo_service",
        "OperationUndoService",
    ),
    # ── services/ domain subdirs: rebac ──────────────────────────────────
    (
        "nexus.services.rebac_service",
        "nexus.services.rebac.rebac_service",
        "ReBACService",
    ),
    (
        "nexus.services.rebac_share_mixin",
        "nexus.services.rebac.rebac_share_mixin",
        "ReBACShareMixin",
    ),
    # ── services/ domain subdirs: skills ─────────────────────────────────
    (
        "nexus.services.skill_service",
        "nexus.services.skills.skill_service",
        "SkillService",
    ),
    # ── Underscore-prefixed (test-visible) names ─────────────────────────
    (
        "nexus.services.sync_service",
        "nexus.system_services.sync.sync_service",
        "_belongs_to_other_mount",
    ),
    (
        "nexus.services.context_branch",
        "nexus.system_services.workspace.context_branch",
        "_BASE_BACKOFF_MS",
    ),
    (
        "nexus.services.context_branch",
        "nexus.system_services.workspace.context_branch",
        "_MAX_RETRIES",
    ),
    (
        "nexus.services.context_branch",
        "nexus.system_services.workspace.context_branch",
        "_slugify",
    ),
    (
        "nexus.services.search_service",
        "nexus.services.search.search_service",
        "DEFAULT_IGNORE_PATTERNS",
    ),
    (
        "nexus.services.search_service",
        "nexus.services.search.search_service",
        "_filter_ignored_paths",
    ),
    (
        "nexus.services.search_service",
        "nexus.services.search.search_service",
        "_should_ignore_path",
    ),
    (
        "nexus.services.brick_reconciler",
        "nexus.system_services.lifecycle.brick_reconciler",
        "_JITTER_MAX",
    ),
]


def _test_id(case: tuple[str, str, str]) -> str:
    """Generate a readable test ID from (old, new, name)."""
    old_mod, _, name = case
    # Use short module tail + name for readability
    short = old_mod.rsplit(".", 1)[-1]
    return f"{short}::{name}"


@pytest.mark.parametrize(
    "old_module,new_module,name",
    _SHIM_CASES,
    ids=[_test_id(c) for c in _SHIM_CASES],
)
def test_shim_identity(old_module: str, new_module: str, name: str) -> None:
    """The shim module re-exports the exact same object as the canonical module."""
    try:
        old_mod = importlib.import_module(old_module)
    except ImportError:
        pytest.skip(f"Cannot import shim module: {old_module}")

    try:
        new_mod = importlib.import_module(new_module)
    except ImportError:
        pytest.skip(f"Cannot import canonical module: {new_module}")

    if not hasattr(new_mod, name):
        pytest.fail(f"Canonical module {new_module} does not export {name!r}")

    if not hasattr(old_mod, name):
        pytest.fail(f"Shim module {old_module} does not re-export {name!r} from {new_module}")

    canonical_obj = getattr(new_mod, name)
    shim_obj = getattr(old_mod, name)

    assert shim_obj is canonical_obj, (
        f"Shim {old_module}.{name} is not the same object as "
        f"{new_module}.{name}.\n"
        f"  shim id:      {id(shim_obj)}\n"
        f"  canonical id: {id(canonical_obj)}\n"
        f"  shim repr:      {shim_obj!r}\n"
        f"  canonical repr: {canonical_obj!r}"
    )
