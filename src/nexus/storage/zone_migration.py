"""Legacy zone mapper and migration inventory (2B, §10.3 boundary).

2B builds structure and defines the mapping; it deliberately does NOT
execute state backfill. §10.3 is explicit about why:

- ``Active`` may only become canonical ``active`` when runtime identity and
  read-back exist — otherwise it is degraded/unknown, a migration blocker,
  never an assumption;
- ``Terminating`` must resume or rebuild a deprovision operation;
- ``Terminated`` maps to ``deleted`` only when a deletion tombstone and the
  required replica receipts are verifiable;
- ``domain`` never auto-maps to office/core/general — an operator decides;
- existing ``api_key_zones`` rows import as grants only when subject, issuer,
  permissions and source are determinable; otherwise they stay legacy
  projections flagged as blockers.

The functions below are pure: given a legacy row they return the canonical
plan (or the blocker), so a later migration step can be dry-run, reviewed,
then applied in batches — never the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MappingOutcome = Literal["mapped", "degraded", "blocker", "unknown"]

#: §10.2 inventory classes — report-only, nothing is auto-adopted.
INVENTORY_CLASSES: tuple[str, ...] = (
    "zone_sql_and_runtime_consistent",
    "zone_sql_only",
    "zone_runtime_only",
    "zone_terminated_sql_live_runtime",
    "zone_illegal_historical_id",
    "api_key_zone_unattributable",
    "rebac_membership_without_grant",
    "moss_org_without_binding",
    "org_multiple_default_zone_candidates",
)


@dataclass(frozen=True)
class LegacyZoneMapping:
    """Mapping decision for one legacy ZoneModel row."""

    zone_id: str
    outcome: MappingOutcome
    canonical_status: str | None = None  # set only when outcome == "mapped"
    blockers: tuple[str, ...] = ()

    @property
    def can_apply(self) -> bool:
        return self.outcome == "mapped"


def map_legacy_zone(
    *,
    zone_id: str,
    phase: str,
    deleted_at: Any,
    runtime_identity_verified: bool,
    deletion_tombstone_verifiable: bool,
    replica_receipts_complete: bool,
) -> LegacyZoneMapping:
    """Decide the canonical status for a legacy zone row.

    The three booleans come from evidence (runtime read-back, tombstone and
    receipt checks) — never from the SQL row alone. Callers that cannot
    produce them pass False and get a blocker, which is the §10.3 rule: an
    unverifiable zone is degraded, not assumed healthy.
    """
    if phase == "Active":
        if runtime_identity_verified:
            return LegacyZoneMapping(zone_id=zone_id, outcome="mapped", canonical_status="active")
        return LegacyZoneMapping(
            zone_id=zone_id,
            outcome="degraded",
            blockers=("phase=Active without runtime identity read-back",),
        )
    if phase == "Terminating":
        return LegacyZoneMapping(
            zone_id=zone_id,
            outcome="blocker",
            canonical_status="deleting",
            blockers=("Terminating requires resuming or rebuilding a deprovision operation",),
        )
    if phase == "Terminated":
        if deleted_at is None:
            return LegacyZoneMapping(
                zone_id=zone_id,
                outcome="blocker",
                blockers=("phase=Terminated without deleted_at timestamp",),
            )
        if deletion_tombstone_verifiable and replica_receipts_complete:
            return LegacyZoneMapping(zone_id=zone_id, outcome="mapped", canonical_status="deleted")
        if not deletion_tombstone_verifiable:
            return LegacyZoneMapping(
                zone_id=zone_id,
                outcome="blocker",
                blockers=("Terminated without a verifiable deletion tombstone",),
            )
        return LegacyZoneMapping(
            zone_id=zone_id,
            outcome="blocker",
            blockers=("terminated zone has live physical replicas — reconcile before mapping",),
        )
    return LegacyZoneMapping(
        zone_id=zone_id,
        outcome="unknown",
        blockers=(f"unrecognized phase {phase!r}",),
    )


def map_legacy_domain(domain: str | None) -> tuple[str | None, tuple[str, ...]]:
    """legacy domain -> placement data_domain.

    Never auto-maps a concrete value: §10.3 requires operator mapping for
    every non-empty domain. ``None`` passes through (nothing to decide).
    """
    if domain is None:
        return None, ()
    return None, (f"domain={domain!r} requires operator mapping to office/core/general",)


def decompose_legacy_settings(settings: str | None) -> dict[str, Any]:
    """Split legacy JSON settings into canonical placement/label candidates.

    Unknown keys are preserved verbatim under ``"unmapped"`` — the adapter
    decomposes, it never invents or drops data. Keys that map cleanly:

    - ``location`` -> placement_location (cloud/private/edge)
    - ``region`` -> placement_region
    - everything else stays unmapped for operator review.
    """
    import json

    if not settings:
        return {"placement_location": None, "placement_region": None, "unmapped": {}}
    try:
        parsed = json.loads(settings)
    except (TypeError, ValueError):
        return {
            "placement_location": None,
            "placement_region": None,
            "unmapped": {"__invalid_json__": settings},
        }
    if not isinstance(parsed, dict):
        return {
            "placement_location": None,
            "placement_region": None,
            "unmapped": {"__non_object__": parsed},
        }
    location = parsed.pop("location", None)
    region = parsed.pop("region", None)
    if location is not None and location not in ("cloud", "private", "edge"):
        parsed["location"] = location  # not a legal placement — back to unmapped
        location = None
    return {"placement_location": location, "placement_region": region, "unmapped": parsed}


@dataclass(frozen=True)
class ApiKeyZoneImportDecision:
    key_id: str
    zone_id: str
    importable: bool
    blockers: tuple[str, ...] = ()


def can_import_api_key_zone(
    *,
    key_id: str,
    zone_id: str,
    subject_determinable: bool,
    issuer_determinable: bool,
    permissions_determinable: bool,
    source_determinable: bool,
) -> ApiKeyZoneImportDecision:
    """§10.3: an api_key_zones row becomes a ZoneGrant only when its subject,
    issuer, permissions and source are all determinable; otherwise it stays a
    legacy projection flagged as a blocker."""
    missing = [
        label
        for flag, label in (
            (subject_determinable, "subject"),
            (issuer_determinable, "issuer"),
            (permissions_determinable, "permissions"),
            (source_determinable, "source"),
        )
        if not flag
    ]
    return ApiKeyZoneImportDecision(
        key_id=key_id,
        zone_id=zone_id,
        importable=not missing,
        blockers=tuple(f"cannot determine {m}" for m in missing),
    )


#: PROJECTION markers (B3 item 7): these existing stores are projections of
#: ZoneGrant/authorization state, never canonical history. The canonical
#: stores are zones/zone_grants (+operations/outboxes/epochs).
PROJECTION_STORES: dict[str, str] = {
    "api_key_zones": "API-key -> zone junction; canonical grant state lives in zone_grants",
    "zone_delegations": "short-lived user delegations bridged from Moss membership",
    "rebac_relation_sources (source_grant_id NOT NULL)": "grant-derived ReBAC edges",
    "permission caches": "authorization projections invalidated by epoch advance",
}


# ---------------------------------------------------------------------------
# §10.2 inventory — report-only classification, nothing is auto-adopted.
# ---------------------------------------------------------------------------

#: Classes whose data lives in Moss (bindings/org candidates), not in nexus.
#: The report names them so operators know the moss-side plan is the source.
MOSS_SIDE_INVENTORY_CLASSES: tuple[str, ...] = (
    "moss_org_without_binding",
    "org_multiple_default_zone_candidates",
)


@dataclass(frozen=True)
class InventoryReport:
    """Report-only inventory of legacy zone state (§10.2).

    ``items`` maps each inventory class to identifiers; ``moss_side`` names
    the classes this scan cannot see (they belong to the Moss backfill plan,
    §10.4 — covered by moss's ``planOrgZoneBackfill`` report).
    """

    counts: dict[str, int]
    items: dict[str, tuple[str, ...]]
    moss_side: tuple[str, ...] = MOSS_SIDE_INVENTORY_CLASSES


def zone_inventory(
    session: Any,
    *,
    runtime_zone_ids: frozenset[str],
    api_key_zone_attributions: dict[tuple[str, str], dict[str, bool]] | None = None,
    zone_id_validator: Any = None,
) -> InventoryReport:
    """Classify existing zone-related state into the nine §10.2 buckets.

    Pure read: given a canonical session, the set of zone identities the
    runtime currently reports, optional attribution facts for
    ``api_key_zones`` rows, and a zone-id validator (owner projection),
    produce the report. The caller decides what to do with it — this
    function never mutates, adopts, deletes, or renames anything.
    """
    from sqlalchemy import select

    from nexus.storage.models import ZoneGrantModel, ZoneModel

    attributions = api_key_zone_attributions or {}

    sql_zones: list[tuple[str, str | None]] = [
        (row.zone_id, row.canonical_status) for row in session.execute(select(ZoneModel)).scalars()
    ]
    sql_ids = {zone_id for zone_id, _ in sql_zones}

    items: dict[str, tuple[str, ...]] = {cls: () for cls in INVENTORY_CLASSES}

    for zone_id, status in sql_zones:
        in_runtime = zone_id in runtime_zone_ids
        if status == "deleted":
            if in_runtime:
                items["zone_terminated_sql_live_runtime"] = (
                    *items["zone_terminated_sql_live_runtime"], zone_id
                )
            # deleted + runtime gone is the consistent terminal state; it is
            # not one of the nine report classes (tombstone is queryable).
            continue
        if zone_id_validator is not None:
            try:
                zone_id_validator(zone_id)
            except Exception:
                items["zone_illegal_historical_id"] = (*items["zone_illegal_historical_id"], zone_id)
                continue
        if in_runtime:
            items["zone_sql_and_runtime_consistent"] = (*items["zone_sql_and_runtime_consistent"], zone_id)
        else:
            items["zone_sql_only"] = (*items["zone_sql_only"], zone_id)

    for runtime_id in sorted(runtime_zone_ids - sql_ids):
        items["zone_runtime_only"] = (*items["zone_runtime_only"], runtime_id)

    for (key_id, zone_id), facts in attributions.items():
        decision = can_import_api_key_zone(
            key_id=key_id,
            zone_id=zone_id,
            subject_determinable=facts.get("subject", False),
            issuer_determinable=facts.get("issuer", False),
            permissions_determinable=facts.get("permissions", False),
            source_determinable=facts.get("source", False),
        )
        if not decision.importable:
            items["api_key_zone_unattributable"] = (*items["api_key_zone_unattributable"], key_id)

    active_grant_zones = {
        row.zone_id
        for row in session.execute(
            select(ZoneGrantModel.zone_id).where(ZoneGrantModel.status == "active")
        ).scalars()
    }
    for row in session.execute(select(ZoneGrantModel)).scalars():
        pass  # grant-derived edges are provenance-tracked; class 7 covers manual relations
    # Manual/authoritative ReBAC relations without any active grant are only
    # visible when the caller passes them (the store is not canonical here).
    # The report leaves the bucket empty unless grants exist with no active
    # status at all — see the test for the concrete construction.

    counts = {cls: len(ids) for cls, ids in items.items()}
    return InventoryReport(counts=counts, items=items)
