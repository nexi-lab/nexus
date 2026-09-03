"""Zone visibility for enumeration surfaces — list, glob, grep, search (#4740).

Reads and writes are path-addressed: a caller has to know a path, and the
RPC layer prefixes it with the caller's ``/zone/<id>/`` namespace.  List and
search are different — they *enumerate*, so the zone predicate applied to
the candidate set is the tenant boundary.  Before #4740 that predicate
failed open in three places:

* a context with no zone claim was resolved to the ROOT zone and received
  the global view;
* entries tagged with the ROOT zone were visible from every zone;
* admins skipped the zone predicate entirely.

This module is the single source of truth for what a caller may enumerate.
The rules, in order:

1. ``context is None`` — the kernel or an internal caller.  Unrestricted.
2. ``all_zones=True`` — only admins may ask; the caller gets the global
   view and the request is audited by the caller (``audit_all_zones``).
   A non-admin asking for ``all_zones`` is refused, not silently narrowed.
3. Readable zones come from ``zone_perms`` (entries granting ``r`` or
   ``x``), else ``zone_set`` (legacy tokens: read implied), else
   ``zone_id``.  ROOT counts only when explicitly granted or claimed; the
   ``zone_id=root`` placeholder a multi-zone token carries does not make
   root-tagged entries visible to it.
4. No zone claim at all:
   * ``is_system`` — internal service caller.  Unrestricted.
   * ``is_admin`` — the operator's default view is the ROOT zone itself,
     not every zone; cross-zone needs ``all_zones=True``.
   * otherwise — **refused** (``PermissionDeniedError`` → 403).

An entry is visible when the zone embedded in its internal path
(``/zone/<id>/…``, or the legacy ``/zones/<id>/…`` the ReBAC filter chain
also recognises) is readable; entries outside a zone namespace fall back to
their ``zone_id`` column, ``None`` meaning ROOT.  Root-tagged entries are
therefore visible only to callers that can read the ROOT zone.

Two facts about the kernel shape this predicate.  In standalone mode the
kernel stamps every row with the *route's* zone, which is ROOT — so the
column carries no tenant information and the ``/zone/<id>/`` prefix the RPC
layer adds to every remote caller's path IS the tenant boundary.  Internal
service scans (``is_system`` with a zone, e.g. ReBAC directory expansion or a
zone export) still need the root-namespace rows they always saw, so a system
view keeps them while remaining scoped to its zone's namespace.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import PermissionDeniedError

logger = logging.getLogger(__name__)

# ``/zone/<id>/`` is what ``nexus.lib.zone_scoping`` produces; ``/zones/<id>/``
# is the legacy layout ``ZonePreFilterStrategy`` in the ReBAC chain treats as
# zone-scoped, kept in sync so list and ReBAC agree on what a zone path is.
_ZONE_PATH_PREFIXES = ("/zone/", "/zones/")

#: Audit event emitted (via ``nexus.lib.events.emit_audit_event``) whenever an
#: admin enumerates across zones with ``all_zones=True``.
ALL_ZONES_AUDIT_EVENT = "zone.all_zones_enumeration"


def zone_from_path(path: Any) -> str | None:
    """Return the zone embedded in an internal ``/zone/<id>/…`` path, else None."""
    if not isinstance(path, str):
        return None
    for prefix in _ZONE_PATH_PREFIXES:
        if path.startswith(prefix):
            zone = path[len(prefix) :].split("/", 1)[0]
            return zone or None
    return None


def _attr(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def readable_zones_from_perms(zone_perms: Any) -> frozenset[str] | None:
    """Zones granted ``r`` or ``x`` by well-formed ``(zone, perms)`` entries.

    Returns ``None`` when there is no well-formed entry at all (no perms
    information), and an empty set when perms exist but grant no read —
    the latter fails closed, matching ``search_auth.readable_zone_filter``.
    """
    if not zone_perms:
        return None
    well_formed = [
        zp
        for zp in zone_perms
        if isinstance(zp, (list, tuple))
        and len(zp) == 2
        and isinstance(zp[0], str)
        and zp[0]
        and isinstance(zp[1], str)
    ]
    if not well_formed:
        return None
    return frozenset(z for z, p in well_formed if "r" in p or "x" in p)


@dataclass(frozen=True, slots=True)
class ZoneView:
    """The set of zones a caller may enumerate.

    ``zones is None`` means unrestricted (global view).  ``all_zones`` records
    that the global view was granted through the explicit admin flag so the
    caller can audit it.  ``system`` marks an internal service scan: it stays
    scoped to its zone's namespace but keeps the root-namespace rows (column
    ROOT, no ``/zone/<id>/`` prefix) that standalone kernels stamp on every
    write.
    """

    zones: frozenset[str] | None
    all_zones: bool = False
    system: bool = False

    @property
    def unrestricted(self) -> bool:
        return self.zones is None

    def allows(self, entry_zone: Any, path: Any = None) -> bool:
        """Whether an entry with this zone column and internal path is visible."""
        if self.zones is None:
            return True
        path_zone = zone_from_path(path)
        if path_zone is not None:
            return path_zone in self.zones
        zone = entry_zone if isinstance(entry_zone, str) and entry_zone else ROOT_ZONE_ID
        if zone == ROOT_ZONE_ID and self.system:
            return True
        return zone in self.zones


UNRESTRICTED_VIEW = ZoneView(zones=None)


def resolve_zone_view(
    context: Any,
    *,
    all_zones: bool = False,
    operation: str = "list",
) -> ZoneView:
    """Resolve the zones *context* may enumerate; fail closed.

    Accepts an ``OperationContext``, a plain dict with the same keys, or
    ``None`` (kernel / internal caller → unrestricted).

    Raises:
        PermissionDeniedError: for a non-admin ``all_zones`` request, or for
            an authenticated non-admin, non-system caller with no zone claim.
    """
    if context is None:
        return UNRESTRICTED_VIEW

    is_admin = bool(_attr(context, "is_admin", False))
    is_system = bool(_attr(context, "is_system", False))

    if all_zones:
        if not is_admin:
            raise PermissionDeniedError(
                f"{operation}: all_zones=true requires admin privileges (#4740)"
            )
        return ZoneView(zones=None, all_zones=True)

    zone_id = _attr(context, "zone_id")
    if not isinstance(zone_id, str) or not zone_id:
        zone_id = None

    readable = readable_zones_from_perms(_attr(context, "zone_perms"))
    if readable is None:
        zone_set = frozenset(
            z for z in (_attr(context, "zone_set") or ()) if isinstance(z, str) and z
        )
        if zone_set:
            readable = zone_set
        elif zone_id is not None:
            readable = frozenset({zone_id})

    if readable is not None:
        return ZoneView(zones=readable, system=is_system)

    # No zone claim of any kind.
    if is_system:
        return UNRESTRICTED_VIEW
    if is_admin:
        return ZoneView(zones=frozenset({ROOT_ZONE_ID}))
    raise PermissionDeniedError(
        f"{operation} refused: caller has no zone claim. Zone-less callers no longer "
        "receive the root view; send X-Nexus-Zone-ID or use a zone-scoped credential (#4740)"
    )


#: ``(request_id, operation)`` keys already audited — one row per request even
#: when the enumeration recurses (pagination, explicit child routes).  Bounded
#: so a long-lived server never grows it without limit.
_RECENT_AUDITS: OrderedDict[tuple[str, str], None] = OrderedDict()
_RECENT_AUDITS_MAX = 4096
_RECENT_AUDITS_LOCK = threading.Lock()


def _already_audited(request_id: Any, operation: str) -> bool:
    if not isinstance(request_id, str) or not request_id:
        return False
    key = (request_id, operation)
    with _RECENT_AUDITS_LOCK:
        if key in _RECENT_AUDITS:
            return True
        _RECENT_AUDITS[key] = None
        while len(_RECENT_AUDITS) > _RECENT_AUDITS_MAX:
            _RECENT_AUDITS.popitem(last=False)
    return False


def audit_all_zones(context: Any, *, operation: str, path: str | None = None) -> None:
    """Record an admin's explicit cross-zone enumeration.

    Emits :data:`ALL_ZONES_AUDIT_EVENT` to the registered audit sinks and an
    INFO log line so the access is attributable even without a sink.  Nested
    calls within one request (same ``request_id``) are recorded once.
    """
    from nexus.lib.events import emit_audit_event

    if _already_audited(_attr(context, "request_id"), operation):
        return

    payload: dict[str, Any] = {
        "operation": operation,
        "path": path,
        "subject_type": _attr(context, "subject_type"),
        "subject_id": _attr(context, "subject_id") or _attr(context, "user_id"),
        "agent_id": _attr(context, "agent_id"),
        "zone_id": _attr(context, "zone_id"),
        "request_id": _attr(context, "request_id"),
    }
    logger.info(
        "[ZONE-AUDIT] all_zones enumeration op=%s path=%s subject=%s:%s zone=%s request_id=%s",
        operation,
        path,
        payload["subject_type"],
        payload["subject_id"],
        payload["zone_id"],
        payload["request_id"],
    )
    emit_audit_event(ALL_ZONES_AUDIT_EVENT, payload)


__all__ = [
    "ALL_ZONES_AUDIT_EVENT",
    "UNRESTRICTED_VIEW",
    "ZoneView",
    "audit_all_zones",
    "readable_zones_from_perms",
    "resolve_zone_view",
    "zone_from_path",
]
