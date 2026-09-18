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
                blockers=("phase=Terminated without deleted_at timestamp"),
            )
        if deletion_tombstone_verifiable and replica_receipts_complete:
            return LegacyZoneMapping(zone_id=zone_id, outcome="mapped", canonical_status="deleted")
        if not deletion_tombstone_verifiable:
            return LegacyZoneMapping(
                zone_id=zone_id,
                outcome="blocker",
                blockers=("Terminated without a verifiable deletion tombstone"),
            )
        return LegacyZoneMapping(
            zone_id=zone_id,
            outcome="blocker",
            blockers=("terminated zone has live physical replicas — reconcile before mapping"),
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
