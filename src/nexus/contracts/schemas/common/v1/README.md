# Product ResourceRef v1 (provisional)

This directory is the Nexus-owned editable source for the Product `ResourceRef`
wire object. The exact nexus-vfs primitive closure is pinned to immutable commit
`24f6730ec90fab8a035b2f5400ed313bda343fbe` in `nexus-vfs.source-lock.json`.
The ResourceRef baseline remains **unfrozen** only because this Nexus owner source is
uncommitted under the work-item authorization. No ZoneId or ZonePath regex, length,
reserved-value, or normalization rule is copied here.

## Wire semantics

- `api_version` is exactly `common.sudo.dev/v1`; unknown majors are rejected.
- `kind` is exactly `ResourceRef`.
- `zone_id + path` form a Product locator. The locator is not a persistent UUID,
  rename-stable identity, ReBAC entity, credential, capability, or grant.
- `zone_id` uses the exact-pinned nexus-vfs lexical reference projection. It
  intentionally accepts `root`; tenant creation applies reserved constants separately,
  and Product authorization remains a distinct check.
- `path` uses the exact-pinned strict nexus-vfs ZonePath projection and required
  `sudoZonePath` vocabulary. It is absolute within `zone_id`, never a global
  `/{zone_id}/...` path or backend key, and is never automatically normalized.
- `version`, when present, is an opaque visible-ASCII owner-issued resource revision
  selector. Absence means no revision pin; null and empty values are invalid.
- `digest`, when present, is algorithm-qualified lowercase hexadecimal data describing
  the logical bytes obtained by dereferencing the selected resource. `sha256` and
  `blake3` values are exactly 32 bytes (64 hex characters). It is not inferred from the
  current Nexus `content_id` field.
- `media_type` is a canonical lowercase type/subtype without parameters.
- `size_bytes` is a canonical non-negative decimal string. Zero means a known empty
  resource; absence means unknown. The string representation avoids JavaScript/Rust
  integer-width disagreement.
- Unknown optional top-level fields are accepted and preserved by the Python adapter
  when they use bounded snake_case metadata values. Known optional fields reject
  JSON null. Unknown metadata may contain null.

## Security boundary

A valid ResourceRef only identifies a target. Every dereference must authenticate the
principal and reauthorize the target `zone_id + path` under current policy before any
routing or storage operation. Validation never grants access. Resource bytes, Secret
bytes, credential material, and large payloads do not belong in this object.

The contract does not resolve a validated `(zone_id, path)` to a mount or backend.
Nexus Product services retain responsibility for authorization and routing. Behavior
for zero matching mount aliases, multiple matching aliases, root fallback, and the
ordering/content of permission evaluation is explicitly deferred to a separate
resolver design; the adapter must not guess or normalize those outcomes.

The extension policy rejects obvious secret-like property names and bounds structural
metadata. It is a guard against accidental embedding, not a secret scanner: an
innocuously named value can still be sensitive and must be governed by producer
review and redaction.

## Actual and deferred boundaries

Current Nexus has no production `ResourceRef` type or runtime boundary. Existing file
metadata, SQL path projections, URNs, ReBAC entities, VFS paths, and service DTOs have
different identities and version/digest conventions. This adapter is owner conformance
preparation only; it is not consumer adoption.

Product Zone/ZoneGrant persistence, lifecycle, APIs, ReBAC behavior, migration,
runtime writers/stores, and deployment are deferred. The adjacent manifest and source
lock record the immutable nexus-vfs closure and the still-pending immutable Nexus owner
revision.
