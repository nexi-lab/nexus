"""Derive readable zone scopes for the HTTP query and batch adapters.

The adapters intersect credential grants with requested zones before sending
queries to the Search host, which enforces zone and file permissions.
"""

from typing import Any


def readable_zone_filter(
    zone_set: Any,
    zone_perms: Any,
) -> frozenset[str] | None:
    """Derive a federated zone allow-list from a credential's grants.

    Search is a READ — write-only zone grants must not be searchable
    (Issue #4542 round-8 review).  When per-zone perms are known, only
    WELL-FORMED entries are consulted — ``isinstance(zp, (list, tuple))
    and len(zp) == 2 and isinstance(zp[0], str) and isinstance(zp[1],
    str)`` (Issue #4557: a malformed entry, e.g. ``None`` or a truthy
    non-str member, must neither crash ``len()`` nor stringify into a
    false positive, e.g. ``str(True)`` containing "r") — and only zones
    granting ``r`` or ``x`` pass.  An explicit grant list where every
    entry is malformed, or none is readable, fails CLOSED (empty
    frozenset → zero searchable zones) — this includes the case where
    ``zone_perms`` is present but entirely malformed, which must NOT
    fall back to the raw ``zone_set``.  Credentials with a zone list but
    no perms info at all keep the whole list (legacy tokens predate
    per-zone perms).  Returns ``None`` (unbounded) only when the
    credential carries no explicit zone grant at all.
    """
    if zone_perms:
        return frozenset(
            zp[0]
            for zp in zone_perms
            if isinstance(zp, (list, tuple))
            and len(zp) == 2
            and isinstance(zp[0], str)
            and isinstance(zp[1], str)
            and ("r" in zp[1] or "x" in zp[1])
        )
    if zone_set:
        return frozenset(str(z) for z in zone_set)
    return None


def token_zone_filter_from_auth(
    auth_result: dict[str, Any],
    *,
    root_zone_id: str,
) -> frozenset[str] | None:
    """Derive the search-readable token allow-list from an auth result.

    Issue #4542 rounds 8-10: search is a READ.  Admin and unconstrained
    (empty-scope) credentials return ``None`` (unbounded — #4541
    exemption); otherwise only zones granting ``r``/``x`` pass, and an
    explicit grant list with no readable zone yields the empty set
    (callers fail closed on it).

    Issue #4557 (gap 3): a root-only ``zone_set`` is normally treated as
    unbounded too (root grants cross-zone access and its id never
    intersects concrete zone names), but an EXPLICIT non-read root grant
    must not be unbounded — a ``root:"w"`` credential must not be able to
    search everything.  When ``zone_set == {root_zone_id}``, every
    WELL-FORMED root entry in ``zone_perms`` (same parsing discipline as
    ``readable_zone_filter``) is aggregated by UNION — not first-match —
    so the outcome is order-independent across duplicate entries: if no
    well-formed root entry exists at all (legacy tokens / absent perms),
    the exemption stands unchanged (``None``); if any entry grants ``r``
    or ``x``, it stands (``None``); otherwise every root entry is
    write-only and the credential fails CLOSED (empty frozenset — the
    router's existing empty-filter 403 fires from there).
    """
    if auth_result.get("is_admin", False):
        return None
    raw_zone_set = tuple(auth_result.get("zone_set") or ())
    if not raw_zone_set:
        # Issue #4740: an empty scope is "unbounded" only for a credential
        # that at least carries a zone claim (a root-zone operator key).  A
        # non-admin credential with NO zone claim at all used to fall through
        # to the root index here; it now fails closed (empty set → the
        # router's existing 403), matching sys_readdir's refusal of
        # zone-less callers.
        return None if auth_result.get("zone_id") else frozenset()
    if set(raw_zone_set) == {root_zone_id}:
        root_letters = "".join(
            zp[1]
            for zp in (auth_result.get("zone_perms") or ())
            if isinstance(zp, (list, tuple))
            and len(zp) == 2
            and isinstance(zp[0], str)
            and isinstance(zp[1], str)
            and zp[0] == root_zone_id
        )
        if not root_letters:
            return None  # No explicit root entry: legacy exemption stands.
        return None if ("r" in root_letters or "x" in root_letters) else frozenset()
    return readable_zone_filter(raw_zone_set, auth_result.get("zone_perms"))
