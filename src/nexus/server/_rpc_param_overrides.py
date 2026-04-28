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
    AccessParams,
    SysRenameParams,
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


# ============================================================
# RPC alias params with conservative defaults (Codex adversarial
# review of nexi-lab/nexus#3701)
# ============================================================
#
# These override the auto-generated ``MkdirParams`` / ``RmdirParams``
# when accessed via the ``mkdir`` / ``rmdir`` RPC names so that legacy
# clients sending ``{"path": "/foo"}`` without explicit ``parents`` /
# ``exist_ok`` / ``recursive`` fields get the safe defaults
# (``parents=False``, ``exist_ok=False``, ``recursive=False``) rather
# than the NexusFS signature defaults (``parents=True``,
# ``exist_ok=True``, ``recursive=True``).
#
# Background: ``NexusFS.mkdir`` has ``parents=True, exist_ok=True``
# defaults (mkdir -p), and ``NexusFS.rmdir`` has ``recursive=True``
# (rm -rf). The legacy RPC alias has always been conservative —
# changing the defaults at the RPC layer silently turns a previously
# safe ``rmdir`` call into a destructive recursive subtree delete,
# and ``mkdir`` starts silently creating parent directories and
# succeeding on existing paths. This was caught by Codex during the
# #3701 PR review. These alias classes preserve the pre-#3701
# behavior exactly.


@dataclass
class MkdirAliasParams:
    """Legacy-conservative mkdir RPC alias params.

    NexusFS.mkdir defaults to ``parents=True, exist_ok=True`` (mkdir
    -p), but the ``mkdir`` RPC alias has historically used
    conservative defaults so legacy clients that omit these fields
    get a plain mkdir that errors on missing parents or existing
    paths.
    """

    path: str
    parents: bool = False
    exist_ok: bool = False


@dataclass
class RmdirAliasParams:
    """Legacy-conservative rmdir RPC alias params.

    NexusFS.rmdir defaults to ``recursive=True`` (rm -rf), but the
    ``rmdir`` RPC alias has historically been non-recursive so
    legacy clients that omit ``recursive`` get subtree-safe
    behavior. Dropping this override would turn a previously safe
    rmdir into a destructive recursive delete — a real behavioral
    regression.
    """

    path: str
    recursive: bool = False


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
#
# ``ainitialize_semantic_search`` is a SearchService method that takes an
# ``nx: NexusFS`` argument which cannot be serialized across RPC.  The
# server-side handler ignores the param and injects the server's own
# ``nexus_fs``, so we omit ``nx`` from the RPC params entirely and
# accept only the embedding-pipeline config knobs.
@dataclass
class AInitializeSemanticSearchParams:
    """Parameters for the RPC form of ``ainitialize_semantic_search``.

    The client-side ``nexus search init`` CLI still passes ``nx=nx`` and
    ``record_store_engine=None`` from its local-mode code path.  Accept
    them as ignored optional fields so the dataclass construction
    doesn't fail — the server-side handler injects its own ``nexus_fs``
    for ``nx`` and leaves ``record_store_engine`` alone.
    """

    embedding_provider: str | None = None
    embedding_model: str | None = None
    api_key: str | None = None
    chunk_size: int = 1024
    chunk_strategy: str = "semantic"
    async_mode: bool = True
    cache_url: str | None = None
    embedding_cache_ttl: int = 86400 * 3
    # Ignored — client sends these but the server injects its own nx
    nx: Any | None = None
    record_store_engine: Any | None = None


@dataclass
class LockAcquireParams:
    """Parameters for lock_acquire(): Tier 2 wrapper over sys_lock that returns a dict over gRPC.

    No matching Python ``def lock_acquire`` exists — the dispatcher routes
    `lock_acquire` straight to `handle_lock_acquire` in
    `server/rpc/handlers/filesystem.py`, which calls `nexus_fs.sys_lock`
    and shapes the result as ``{"acquired": bool, "lock_id": str}``.
    """

    path: str
    mode: str = "exclusive"
    max_holders: int = 1
    ttl: float = 30.0


# ============================================================
# Override METHOD_PARAMS entries for all override classes
# ============================================================

OVERRIDE_METHOD_PARAMS: dict[str, type] = {
    "sys_read": ReadParams,
    # Short aliases for nexus-test / remote clients. Keys are alias names
    # (not the canonical method on NexusFS) — the canonical method's
    # generated params class is reused here so the alias and canonical
    # forms accept the same params.
    "read": ReadParams,
    "write": WriteParams,
    "delete": SysUnlinkParams,
    "exists": AccessParams,
    "rename": SysRenameParams,
    # Codex review of #3701: restore legacy-conservative mkdir/rmdir
    # alias defaults. NexusFS.mkdir defaults to ``parents=True,
    # exist_ok=True`` (mkdir -p) and NexusFS.rmdir defaults to
    # ``recursive=True`` (rm -rf), but the RPC alias has always been
    # conservative. Using the auto-generated classes would silently
    # flip legacy clients sending only ``{"path": ...}`` into mkdir-p
    # semantics for mkdir and recursive subtree delete for rmdir — a
    # real behavioral regression flagged by Codex.
    "mkdir": MkdirAliasParams,
    "rmdir": RmdirAliasParams,
    # ``sys_rmdir`` is also kept as an alias for older remote clients
    # (``nexus.backends.storage.remote`` still calls ``_call_rpc(
    # "sys_rmdir", ...)``). It shares the same conservative defaults
    # so both spellings behave identically. The dispatch table at
    # ``nexus.server.rpc.dispatch`` registers ``sys_rmdir`` as a
    # backward-compat alias to ``handle_rmdir``.
    "sys_rmdir": RmdirAliasParams,
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
    # Semantic search init (Issue #3728 follow-up — the client calls this
    # via RemoteServiceProxy, and without an entry here ``parse_method_params``
    # rejects the RPC as "Unknown method".)
    "ainitialize_semantic_search": AInitializeSemanticSearchParams,
    # Tier 2 lock_acquire — wraps sys_lock with a dict return for gRPC. There
    # is no Python `def lock_acquire` decorated with @rpc_expose (the kernel
    # owns the syscall and the dispatcher maps lock_acquire → handle_lock_acquire),
    # so the param class lives here as an override to satisfy parse_method_params.
    "lock_acquire": LockAcquireParams,
}
