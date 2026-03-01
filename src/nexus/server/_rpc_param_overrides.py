"""Manual RPC Param overrides — classes that cannot be auto-generated.

These classes are imported AFTER ``_rpc_params_generated.py`` by ``protocol.py``,
so they **replace** any generated version with the same name.

Categories:
  1. RPC-only fields (ReadParams — return_url, expires_in)
  2. Constant defaults (OAuthGetAuthUrlParams, OAuthExchangeCodeParams)
  3. Methods not on NexusFS class (admin, memory, skills, trajectory, etc.)
"""

from dataclasses import dataclass
from typing import Any

from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI
from nexus.server._rpc_params_generated import (
    SysAccessParams,
    SysIsDirectoryParams,
    SysMkdirParams,
    SysRenameParams,
    SysRmdirParams,
    SysUnlinkParams,
    SysWriteParams,
)

# ============================================================
# 1. RPC-only field overrides
# ============================================================


@dataclass
class ReadParams:
    """Parameters for read() method.

    Override: adds RPC-only fields return_url and expires_in
    (not in NexusFS.read() signature, used by handle_read_async).
    """

    path: str
    return_metadata: bool = False
    parsed: bool = False
    return_url: bool = False
    expires_in: int = 3600


# ============================================================
# 2. Constant-default overrides
# ============================================================


@dataclass
class OAuthGetAuthUrlParams:
    """Parameters for oauth_get_auth_url method."""

    provider: str
    redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI
    scopes: list[str] | None = None


@dataclass
class OAuthExchangeCodeParams:
    """Parameters for oauth_exchange_code method."""

    provider: str
    code: str
    user_email: str | None = None
    state: str | None = None
    redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI


# ============================================================
# 3. Admin API Parameters (not on NexusFS class)
# ============================================================


@dataclass
class AdminCreateKeyParams:
    """Parameters for admin_create_key() method."""

    name: str
    zone_id: str
    user_id: str | None = None
    is_admin: bool = False
    expires_days: int | None = None
    subject_type: str = "user"
    subject_id: str | None = None


@dataclass
class AdminListKeysParams:
    """Parameters for admin_list_keys() method."""

    user_id: str | None = None
    zone_id: str | None = None
    is_admin: bool | None = None
    include_revoked: bool = False
    include_expired: bool = False
    limit: int = 100
    offset: int = 0


@dataclass
class AdminGetKeyParams:
    """Parameters for admin_get_key() method."""

    key_id: str
    zone_id: str | None = None


@dataclass
class AdminRevokeKeyParams:
    """Parameters for admin_revoke_key() method."""

    key_id: str
    zone_id: str | None = None


@dataclass
class AdminUpdateKeyParams:
    """Parameters for admin_update_key() method."""

    key_id: str
    zone_id: str | None = None
    expires_days: int | None = None
    is_admin: bool | None = None
    name: str | None = None


@dataclass
class AdminGcVersionsParams:
    """Parameters for admin_gc_versions() method."""

    dry_run: bool = True
    retention_days: int | None = None
    max_versions: int | None = None


@dataclass
class AdminGcVersionsStatsParams:
    """Parameters for admin_gc_versions_stats() method."""

    pass


# ============================================================
# 4. Memory API Parameters (not on NexusFS class)
# ============================================================


@dataclass
class StoreMemoryParams:
    """Parameters for store_memory() method."""

    content: str
    memory_type: str = "fact"
    scope: str = "agent"
    importance: float = 0.5
    namespace: str | None = None
    path_key: str | None = None
    state: str = "active"
    tags: list[str] | None = None


@dataclass
class RetrieveMemoryParams:
    """Parameters for retrieve_memory() method."""

    namespace: str | None = None
    path_key: str | None = None
    path: str | None = None


@dataclass
class DeleteMemoryParams:
    """Parameters for delete_memory() method."""

    memory_id: str


@dataclass
class ListMemoriesParams:
    """Parameters for list_memories() method."""

    limit: int = 50
    scope: str | None = None
    memory_type: str | None = None
    namespace: str | None = None
    namespace_prefix: str | None = None
    state: str | None = "active"


@dataclass
class ApproveMemoryParams:
    """Parameters for approve_memory() method."""

    memory_id: str


@dataclass
class DeactivateMemoryParams:
    """Parameters for deactivate_memory() method."""

    memory_id: str


@dataclass
class ApproveMemoryBatchParams:
    """Parameters for approve_memory_batch() method."""

    memory_ids: list[str]


@dataclass
class DeactivateMemoryBatchParams:
    """Parameters for deactivate_memory_batch() method."""

    memory_ids: list[str]


@dataclass
class DeleteMemoryBatchParams:
    """Parameters for delete_memory_batch() method."""

    memory_ids: list[str]


@dataclass
class QueryMemoriesParams:
    """Parameters for query_memories() method."""

    memory_type: str | None = None
    scope: str | None = None
    state: str | None = "active"
    limit: int = 50
    query: str | None = None
    search_mode: str | None = None
    embedding_provider: str | None = None


# ============================================================
# 5. Trajectory / Playbook Parameters (not on NexusFS class)
# ============================================================


@dataclass
class StartTrajectoryParams:
    """Parameters for start_trajectory() method."""

    task_description: str
    task_type: str | None = None


@dataclass
class LogTrajectoryStepParams:
    """Parameters for log_trajectory_step() method."""

    trajectory_id: str
    step_type: str
    description: str
    result: Any = None


@dataclass
class CompleteTrajectoryParams:
    """Parameters for complete_trajectory() method."""

    trajectory_id: str
    status: str
    success_score: float | None = None
    error_message: str | None = None


@dataclass
class GetPlaybookParams:
    """Parameters for get_playbook() method."""

    playbook_name: str = "default"


@dataclass
class CuratePlaybookParams:
    """Parameters for curate_playbook() method."""

    reflection_memory_ids: list[str]
    playbook_name: str = "default"
    merge_threshold: float = 0.7


@dataclass
class BatchReflectParams:
    """Parameters for batch_reflect() method."""

    agent_id: str | None = None
    since: str | None = None
    min_trajectories: int = 10
    task_type: str | None = None


@dataclass
class QueryTrajectoriesParams:
    """Parameters for query_trajectories() method."""

    agent_id: str | None = None
    status: str | None = None
    limit: int = 50


@dataclass
class QueryPlaybooksParams:
    """Parameters for query_playbooks() method."""

    agent_id: str | None = None
    scope: str | None = None
    limit: int = 50


@dataclass
class ProcessRelearningParams:
    """Parameters for process_relearning() method."""

    limit: int = 10


# ============================================================
# 6. RemoteMetastore Parameters (metadata proxy for REMOTE profile)
# ============================================================


@dataclass
class SetMetadataParams:
    """Parameters for set_metadata() — store/update file metadata.

    Called by RemoteMetastore.put() to persist DT_MOUNT entries and
    metadata updates from the REMOTE deployment profile.
    """

    path: str
    metadata: dict[str, Any] | None = None
    consistency: str = "sc"


# ============================================================
# 8. Namespace override (RPC name differs from method name)
# ============================================================


@dataclass
class NamespaceGetParams:
    """Parameters for namespace_get() method."""

    object_type: str


# ============================================================
# Override METHOD_PARAMS entries for all override classes
# ============================================================

OVERRIDE_METHOD_PARAMS: dict[str, type] = {
    "sys_read": ReadParams,
    # Short aliases for nexus-test / remote clients
    "read": ReadParams,
    "write": SysWriteParams,
    "delete": SysUnlinkParams,
    "exists": SysAccessParams,
    "mkdir": SysMkdirParams,
    "rmdir": SysRmdirParams,
    "rename": SysRenameParams,
    "is_directory": SysIsDirectoryParams,
    "oauth_get_auth_url": OAuthGetAuthUrlParams,
    "oauth_exchange_code": OAuthExchangeCodeParams,
    # Admin
    "admin_create_key": AdminCreateKeyParams,
    "admin_list_keys": AdminListKeysParams,
    "admin_get_key": AdminGetKeyParams,
    "admin_revoke_key": AdminRevokeKeyParams,
    "admin_update_key": AdminUpdateKeyParams,
    "admin_gc_versions": AdminGcVersionsParams,
    "admin_gc_versions_stats": AdminGcVersionsStatsParams,
    # Memory
    "store_memory": StoreMemoryParams,
    "retrieve_memory": RetrieveMemoryParams,
    "delete_memory": DeleteMemoryParams,
    "list_memories": ListMemoriesParams,
    "approve_memory": ApproveMemoryParams,
    "deactivate_memory": DeactivateMemoryParams,
    "approve_memory_batch": ApproveMemoryBatchParams,
    "deactivate_memory_batch": DeactivateMemoryBatchParams,
    "delete_memory_batch": DeleteMemoryBatchParams,
    "query_memories": QueryMemoriesParams,
    # Trajectory / Playbook
    "start_trajectory": StartTrajectoryParams,
    "log_trajectory_step": LogTrajectoryStepParams,
    "complete_trajectory": CompleteTrajectoryParams,
    "get_playbook": GetPlaybookParams,
    "curate_playbook": CuratePlaybookParams,
    "batch_reflect": BatchReflectParams,
    "query_trajectories": QueryTrajectoriesParams,
    "query_playbooks": QueryPlaybooksParams,
    "process_relearning": ProcessRelearningParams,
    # Namespace
    "namespace_get": NamespaceGetParams,
    # RemoteMetastore
    "sys_setattr": SetMetadataParams,
}
