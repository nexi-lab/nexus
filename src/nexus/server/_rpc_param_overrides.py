"""Manual RPC Param overrides — classes that cannot be auto-generated.

Methods listed in ``OVERRIDE_METHOD_PARAMS`` are skipped by
``scripts/generate_rpc_params.py`` so ``protocol.py`` can re-export these
hand-written Param classes and route parsing through them.

Kernel syscalls (sys_*, mkdir, rmdir, access, is_directory, locks +
aliases) DO NOT need overrides — they go through the thin dispatch in
``nexus.server._kernel_syscall_dispatch`` which decodes the wire dict
straight into the NexusFS method via ``inspect.signature``, no Param
dataclass involved.  Categories below are limited to the surfaces
that still flow through ``dispatch.py`` + ``parse_method_params``:

  1. Constant defaults (OAuthGetAuthUrlParams, OAuthExchangeCodeParams)
  2. Methods not on NexusFS class (admin, etc.)
  3. RPC-name → method-name overrides where the wire-name doesn't
     match a Python signature
"""

from dataclasses import dataclass, field
from typing import Any

from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI, ROOT_ZONE_ID

# ============================================================
# 1. Constant-default overrides
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
    zone_id: str = ROOT_ZONE_ID
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
class HubAdminTokenCreateParams:
    """Parameters for hub_admin_token_create() method."""

    name: str
    zones: str | None = None
    zones_glob: str | None = None
    admin: bool = False
    expires: str | None = None
    user_id: str | None = None


@dataclass
class HubAdminTokenListParams:
    """Parameters for hub_admin_token_list() method."""

    show_revoked: bool = False


@dataclass
class HubAdminTokenRevokeParams:
    """Parameters for hub_admin_token_revoke() method."""

    identifier: str


@dataclass
class HubAdminStatusParams:
    """Parameters for hub_admin_status() method."""

    pass


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
# 4. ReBAC RPC compatibility aliases
# ============================================================


@dataclass
class RevokeShareParams:
    """Parameters for revoke_share(), including HTTP/JSON alias fields."""

    resource: tuple[str, str]
    target: tuple[str, str] | None = None
    target_user: str | None = None
    target_group: str | None = None
    permission: str = "viewer"
    zone_id: str | None = None
    context: Any = None

    def __post_init__(self) -> None:
        if isinstance(self.resource, list):
            object.__setattr__(self, "resource", tuple(self.resource))
        if isinstance(self.target, list):
            object.__setattr__(self, "target", tuple(self.target))


@dataclass
class RevokeShareByIdParams:
    """Parameters for revoke_share_by_id(), accepting share_id as tuple_id."""

    tuple_id: str | None = None
    share_id: str | None = None
    context: Any = None


# ============================================================
# 8. Namespace override (RPC name differs from method name)
# ============================================================


@dataclass
class NamespaceGetParams:
    """Parameters for namespace_get() method."""

    object_type: str


# ============================================================
# 9. Semantic search initialization override
# ============================================================
# Override METHOD_PARAMS entries for all override classes
# ============================================================

OVERRIDE_METHOD_PARAMS: dict[str, type] = {
    "oauth_get_auth_url": OAuthGetAuthUrlParams,
    "oauth_exchange_code": OAuthExchangeCodeParams,
    # Admin
    "admin_create_key": AdminCreateKeyParams,
    "admin_list_keys": AdminListKeysParams,
    "admin_get_key": AdminGetKeyParams,
    "admin_revoke_key": AdminRevokeKeyParams,
    "admin_update_key": AdminUpdateKeyParams,
    "admin_write_permission": AdminWritePermissionParams,
    "hub_admin_token_create": HubAdminTokenCreateParams,
    "hub_admin_token_list": HubAdminTokenListParams,
    "hub_admin_token_revoke": HubAdminTokenRevokeParams,
    "hub_admin_status": HubAdminStatusParams,
    "admin_gc_versions": AdminGcVersionsParams,
    "admin_gc_versions_stats": AdminGcVersionsStatsParams,
    # ReBAC aliases
    "revoke_share": RevokeShareParams,
    "revoke_share_by_id": RevokeShareByIdParams,
    # Namespace
    "namespace_get": NamespaceGetParams,
}
