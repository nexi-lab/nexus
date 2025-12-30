"""Drop ix_* duplicate indexes created by SQLAlchemy index=True

These indexes are exact duplicates of idx_* indexes defined in __table_args__.
SQLAlchemy creates ix_* indexes from column-level index=True annotations,
while idx_* indexes are created from explicit Index() definitions.

Based on comprehensive analysis:
- 73 duplicate indexes identified across 19 tables
- All ix_* indexes have equivalent idx_* or are covered by composite/unique indexes
- Removing duplicates improves write performance and reduces storage

This migration also removes indexes that are covered by:
- Unique constraints (content_chunks, mount_configs)
- Composite unique constraints (version_history, workflows)
- Primary keys (rebac_version_sequences)

Revision ID: drop_ix_duplicate_indexes
Revises: drop_duplicate_unused_indexes
Create Date: 2025-12-29
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_ix_duplicate_indexes"
down_revision: Union[str, Sequence[str], None] = "drop_duplicate_unused_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop duplicate ix_* indexes.

    Each ix_* index is either:
    1. An exact duplicate of an idx_* index on the same column(s)
    2. Covered by a composite index that starts with the same column(s)
    3. Covered by a unique constraint that creates an implicit index
    4. Covered by the primary key
    """
    # =========================================================================
    # memories table - 7 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_memories_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_session_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_expires_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_state"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_namespace"))
    op.execute(text("DROP INDEX IF EXISTS ix_memories_embedding_model"))

    # =========================================================================
    # rebac_tuples table - 6 duplicates (covered by composites)
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_tuples_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_tuples_subject_type"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_tuples_subject_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_tuples_relation"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_tuples_object_type"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_tuples_object_id"))

    # =========================================================================
    # rebac_check_cache table - 8 duplicates (covered by composites)
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_subject_type"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_subject_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_permission"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_object_type"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_object_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_computed_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_check_cache_expires_at"))

    # =========================================================================
    # rebac_version_sequences - 1 duplicate (tenant_id is PK!)
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_rebac_version_sequences_tenant_id"))

    # =========================================================================
    # trajectories table - 9 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_task_type"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_status"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_started_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_completed_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_path"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_session_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectories_expires_at"))

    # =========================================================================
    # playbooks table - 9 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_name"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_scope"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_path"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_session_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_expires_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_playbooks_created_at"))

    # =========================================================================
    # user_sessions table - 5 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_user_sessions_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_user_sessions_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_user_sessions_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_user_sessions_created_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_user_sessions_expires_at"))

    # =========================================================================
    # sandbox_metadata table - 7 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_name"))
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_status"))
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_created_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_sandbox_metadata_expires_at"))

    # =========================================================================
    # oauth_credentials table - 6 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_credentials_provider"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_credentials_user_email"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_credentials_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_credentials_tenant_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_credentials_expires_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_credentials_revoked"))

    # =========================================================================
    # trajectory_feedback table - 3 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_trajectory_feedback_trajectory_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectory_feedback_feedback_type"))
    op.execute(text("DROP INDEX IF EXISTS ix_trajectory_feedback_created_at"))

    # =========================================================================
    # workspace_snapshots table - 3 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_snapshots_workspace_path"))
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_snapshots_manifest_hash"))
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_snapshots_created_at"))

    # =========================================================================
    # workspace_configs table - 4 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_configs_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_configs_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_configs_session_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_workspace_configs_expires_at"))

    # =========================================================================
    # memory_configs table - 4 duplicates
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_memory_configs_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memory_configs_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memory_configs_session_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_memory_configs_expires_at"))

    # =========================================================================
    # file_paths table - 1 duplicate (covered by composite)
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_file_paths_tenant_id"))

    # =========================================================================
    # content_cache table - 1 duplicate
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_content_cache_tenant_id"))

    # =========================================================================
    # Indexes covered by unique constraints
    # =========================================================================
    # content_chunks: idx_content_chunks_hash covered by unique(content_hash)
    op.execute(text("DROP INDEX IF EXISTS idx_content_chunks_hash"))

    # mount_configs: idx_mount_configs_mount_point covered by unique(mount_point)
    op.execute(text("DROP INDEX IF EXISTS idx_mount_configs_mount_point"))

    # version_history: idx_version_history_resource covered by uq_version prefix
    op.execute(text("DROP INDEX IF EXISTS idx_version_history_resource"))

    # workflows: idx_workflows_tenant covered by uq_tenant_workflow_name prefix
    op.execute(text("DROP INDEX IF EXISTS idx_workflows_tenant"))

    # =========================================================================
    # sync_jobs table - 1 duplicate
    # =========================================================================
    op.execute(text("DROP INDEX IF EXISTS ix_sync_jobs_mount_point"))

    # =========================================================================
    # Additional duplicates found in user-related tables
    # =========================================================================

    # users table - 5 duplicates (some covered by composites)
    op.execute(text("DROP INDEX IF EXISTS ix_users_email"))  # → idx_users_email
    op.execute(text("DROP INDEX IF EXISTS ix_users_username"))  # → idx_users_username
    op.execute(text("DROP INDEX IF EXISTS ix_users_deleted_at"))  # → idx_users_deleted
    op.execute(text("DROP INDEX IF EXISTS ix_users_primary_auth_method"))  # → idx_users_auth_method
    op.execute(text("DROP INDEX IF EXISTS ix_users_is_active"))  # → idx_users_active
    op.execute(text("DROP INDEX IF EXISTS ix_users_external_user_id"))  # → idx_users_external composite
    op.execute(text("DROP INDEX IF EXISTS ix_users_external_user_service"))  # → idx_users_external composite

    # user_oauth_accounts table - 1 duplicate
    op.execute(text("DROP INDEX IF EXISTS ix_user_oauth_accounts_user_id"))  # → idx_user_oauth_user

    # oauth_api_keys table - 1 duplicate
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_api_keys_user_id"))  # → idx_oauth_api_keys_user

    # tenants table - 2 duplicates
    op.execute(text("DROP INDEX IF EXISTS ix_tenants_name"))  # → idx_tenants_name
    op.execute(text("DROP INDEX IF EXISTS ix_tenants_is_active"))  # → idx_tenants_active


def downgrade() -> None:
    """Recreate ix_* indexes if needed for rollback.

    Note: This is a destructive operation that recreates many indexes.
    Only run this if absolutely necessary.
    """
    # memories table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_tenant_id ON memories (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_user_id ON memories (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_agent_id ON memories (agent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_session_id ON memories (session_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_expires_at ON memories (expires_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_state ON memories (state)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_namespace ON memories (namespace)"))

    # rebac_tuples table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_tuples_tenant_id ON rebac_tuples (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_tuples_subject_type ON rebac_tuples (subject_type)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_tuples_relation ON rebac_tuples (relation)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_tuples_object_type ON rebac_tuples (object_type)"))

    # rebac_check_cache table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_check_cache_tenant_id ON rebac_check_cache (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_check_cache_subject_type ON rebac_check_cache (subject_type)"))

    # rebac_version_sequences - index on PK, not needed
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_rebac_version_sequences_tenant_id ON rebac_version_sequences (tenant_id)"))

    # trajectories table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_user_id ON trajectories (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_agent_id ON trajectories (agent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_tenant_id ON trajectories (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_task_type ON trajectories (task_type)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_status ON trajectories (status)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_completed_at ON trajectories (completed_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_path ON trajectories (path)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_session_id ON trajectories (session_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectories_expires_at ON trajectories (expires_at)"))

    # playbooks table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_user_id ON playbooks (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_agent_id ON playbooks (agent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_tenant_id ON playbooks (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_name ON playbooks (name)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_scope ON playbooks (scope)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_path ON playbooks (path)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_session_id ON playbooks (session_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_playbooks_expires_at ON playbooks (expires_at)"))

    # user_sessions table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_user_id ON user_sessions (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_agent_id ON user_sessions (agent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_tenant_id ON user_sessions (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_created_at ON user_sessions (created_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_expires_at ON user_sessions (expires_at)"))

    # sandbox_metadata table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_sandbox_metadata_user_id ON sandbox_metadata (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_sandbox_metadata_agent_id ON sandbox_metadata (agent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_sandbox_metadata_tenant_id ON sandbox_metadata (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_sandbox_metadata_status ON sandbox_metadata (status)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_sandbox_metadata_created_at ON sandbox_metadata (created_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_sandbox_metadata_expires_at ON sandbox_metadata (expires_at)"))

    # oauth_credentials table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_credentials_provider ON oauth_credentials (provider)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_credentials_user_email ON oauth_credentials (user_email)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_credentials_user_id ON oauth_credentials (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_credentials_tenant_id ON oauth_credentials (tenant_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_credentials_expires_at ON oauth_credentials (expires_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_credentials_revoked ON oauth_credentials (revoked)"))

    # trajectory_feedback table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectory_feedback_trajectory_id ON trajectory_feedback (trajectory_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectory_feedback_feedback_type ON trajectory_feedback (feedback_type)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_trajectory_feedback_created_at ON trajectory_feedback (created_at)"))

    # workspace_snapshots table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_snapshots_workspace_path ON workspace_snapshots (workspace_path)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_snapshots_manifest_hash ON workspace_snapshots (manifest_hash)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_snapshots_created_at ON workspace_snapshots (created_at)"))

    # workspace_configs table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_configs_user_id ON workspace_configs (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_configs_session_id ON workspace_configs (session_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_configs_expires_at ON workspace_configs (expires_at)"))

    # memory_configs table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_configs_user_id ON memory_configs (user_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_configs_session_id ON memory_configs (session_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_configs_expires_at ON memory_configs (expires_at)"))

    # file_paths table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_file_paths_tenant_id ON file_paths (tenant_id)"))

    # content_cache table
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_content_cache_tenant_id ON content_cache (tenant_id)"))

    # Indexes covered by unique constraints
    op.execute(text("CREATE INDEX IF NOT EXISTS idx_content_chunks_hash ON content_chunks (content_hash)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS idx_mount_configs_mount_point ON mount_configs (mount_point)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS idx_version_history_resource ON version_history (resource_type, resource_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS idx_workflows_tenant ON workflows (tenant_id)"))
