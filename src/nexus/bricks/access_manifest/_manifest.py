"""Access-manifest brick metadata (extension store discovery).

Imported by `nexus.extensions.store` for metadata-only enumeration without
loading the brick impl. The legacy ``brick_factory.py`` constants drive
runtime boot; this manifest is consumed by introspection only.
"""

from __future__ import annotations

from nexus.extensions.manifest import BrickManifest

MANIFEST = BrickManifest(
    name="access_manifest",
    module="nexus.bricks.access_manifest.brick_factory",
    factory="create",
    description="Per-mount access policy compiler service",
    tier="independent",
    result_key="access_manifest_service",
    profile_gate=None,
)
