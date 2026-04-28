"""Subject-keyed zone_perms cache + request-scoped override.

Lives in :mod:`nexus.lib` (not :mod:`nexus.bricks.rebac`) because the
kernel layer (``nexus.core.nexus_fs_*``) and the storage layer
(``nexus.storage.api_key_ops``) need to bind / invalidate request-scope
grants.  Keeping the primitives here preserves the four-tier import
hierarchy: ``contracts < lib+security < kernel < services < bricks``.

The actual ``PermissionEnforcer.check`` consults
:func:`_lookup_zone_perms` to recover the caller's zone allow-list when
the Rust kernel boundary strips ``OperationContext.zone_perms``.

Issue #3786 (sandbox federation hardening).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Bounded by `_ZONE_PERMS_CACHE_MAX` (LRU eviction) and
# `_ZONE_PERMS_CACHE_TTL_SECONDS`.  Without these, a long-running hub
# would (a) grow the cache without bound and (b) keep authorising
# against revoked grants.
_ZONE_PERMS_CACHE_MAX = 1024
_ZONE_PERMS_CACHE_TTL_SECONDS = 300  # 5 min — typical token rotation.
_zone_perms_cache: "OrderedDict[str, tuple[float, tuple]]" = OrderedDict()
_zone_perms_cache_lock = threading.Lock()

# Request-scoped override for zone_perms.  When set, `_lookup_zone_perms`
# returns this value before consulting the subject-keyed cache.  Solves
# cross-token leaks: two API keys for the same subject_id would otherwise
# share a single cache slot and authorise each other's writes on the
# native (Rust-stripped) code path.  The caller (e.g. ContentMixin.write)
# wraps the kernel call in `request_zone_perms_scope(context.zone_perms)`
# so the in-flight request's grants survive the Rust→Python rebuild
# without depending on cross-request cache state.
_request_zone_perms: ContextVar[tuple | None] = ContextVar(
    "_nexus_request_zone_perms", default=None
)


@contextmanager
def request_zone_perms_scope(zone_perms: tuple | None) -> Iterator[None]:
    """Bind zone_perms to the current task for nested kernel→Python hook calls."""
    if not zone_perms:
        yield
        return
    token = _request_zone_perms.set(tuple(zone_perms))
    try:
        yield
    finally:
        _request_zone_perms.reset(token)


def cache_zone_perms(user_id: str, zone_perms: tuple) -> None:
    """Store zone_perms for user_id so they survive the Rust context boundary."""
    if not user_id or not zone_perms:
        return
    now = time.monotonic()
    with _zone_perms_cache_lock:
        # LRU touch — move to end on overwrite.
        _zone_perms_cache.pop(user_id, None)
        _zone_perms_cache[user_id] = (now, zone_perms)
        # Bound size — evict oldest.
        while len(_zone_perms_cache) > _ZONE_PERMS_CACHE_MAX:
            _zone_perms_cache.popitem(last=False)


def lookup_zone_perms(user_id: str) -> tuple | None:
    """Return zone_perms for user_id — request-scope first, else cache (if fresh).

    Request-scoped override (set via `request_zone_perms_scope`) wins so
    two API keys for the same subject can't authorise each other's writes
    through the shared subject-keyed cache slot.  Stale cache entries are
    evicted on lookup so revoked grants don't survive past the TTL.
    """
    req_perms = _request_zone_perms.get()
    if req_perms:
        return req_perms
    if not user_id:
        return None
    now = time.monotonic()
    with _zone_perms_cache_lock:
        entry = _zone_perms_cache.get(user_id)
        if entry is None:
            return None
        ts, perms = entry
        if (now - ts) > _ZONE_PERMS_CACHE_TTL_SECONDS:
            _zone_perms_cache.pop(user_id, None)
            return None
        # LRU touch on access.
        _zone_perms_cache.move_to_end(user_id)
        return perms


def invalidate_zone_perms(user_id: str) -> None:
    """Drop any cached zone_perms for user_id — call on key revocation/grant change."""
    if not user_id:
        return
    with _zone_perms_cache_lock:
        _zone_perms_cache.pop(user_id, None)
