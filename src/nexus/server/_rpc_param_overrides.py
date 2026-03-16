"""Manual RPC Param overrides — classes that cannot be auto-generated.

These classes are imported AFTER ``_rpc_params_generated.py`` by ``protocol.py``,
so they **replace** any generated version with the same name.

Categories:
  1. RPC-only fields (ReadParams — return_url, expires_in)
  2. Constant defaults (OAuthGetAuthUrlParams, OAuthExchangeCodeParams)
  3. Methods not on NexusFS class (admin, memory, skills, trajectory, etc.)
"""

from dataclasses import dataclass, field
from typing import Any

from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI
from nexus.server._rpc_params_generated import (
    SysAccessParams,
    SysIsDirectoryParams,
    SysMkdirParams,
    SysRenameParams,
    SysRmdirParams,
    SysUnlinkParams,
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
    count: int | None = None
    offset: int = 0
    return_metadata: bool = False
    parsed: bool = False
    return_url: bool = False
    expires_in: int = 3600


@dataclass
class WriteParams:
    """Parameters for write() method (Tier 2 convenience).

    Includes POSIX count/offset plus OCC params handled at the RPC layer.
    Lock params removed — use lock()/unlock() explicitly.
    """

    path: str
    buf: bytes | str
    count: int | None = None
    offset: int = 0
    if_match: str | None = None
    if_none_match: bool = False
    force: bool = False


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
class AdminWritePermissionParams:
    """Parameters for admin_write_permission() method.

    Accepts a batch of ReBAC relationship tuples to write.
    Each tuple is a dict with keys: subject, relation, object, zone_id.
    """

    tuples: list[dict[str, Any]] = field(default_factory=list)


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
    "write": WriteParams,
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
    "admin_write_permission": AdminWritePermissionParams,
    "admin_gc_versions": AdminGcVersionsParams,
    "admin_gc_versions_stats": AdminGcVersionsStatsParams,
    # Namespace
    "namespace_get": NamespaceGetParams,
    # RemoteMetastore
    "sys_setattr": SetMetadataParams,
}
