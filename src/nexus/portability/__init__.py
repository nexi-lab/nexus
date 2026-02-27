"""Zone Data Portability module for .nexus bundle export/import.

This module provides complete zone data portability through the .nexus
bundle format, enabling:

- Export zone data (files, metadata, permissions, embeddings) as portable bundle
- Import bundles into another Nexus instance with zone ID remapping
- Cross-zone migration (Company A → Company B)
- GDPR Article 20 compliance (Right to Data Portability)

Example usage:

    # Export zone data
    from nexus.portability import ZoneExportOptions, ExportManifest

    options = ZoneExportOptions(
        output_path="/backup/zone.nexus",
        include_content=True,
        include_permissions=True,
    )
    # Export service creates the bundle

    # Import zone data
    from nexus.portability import ZoneImportOptions, ImportResult

    options = ZoneImportOptions(
        bundle_path="/backup/zone.nexus",
        target_zone_id="new-zone",
        conflict_mode=ConflictMode.SKIP,
    )
    # Import service processes the bundle

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Zone Data Portability
"""

from nexus.portability.bundle import (
    BundleReader,
    inspect_bundle,
    validate_bundle,
)
from nexus.portability.export_service import (
    ZoneExportService,
    export_zone_bundle,
)
from nexus.portability.import_service import (
    ZoneImportService,
    import_zone_bundle,
)
from nexus.portability.models import (
    BUNDLE_EXTENSION,
    BUNDLE_FORMAT_VERSION,
    BUNDLE_PATHS,
    DEFAULT_COMPRESSION_LEVEL,
    DEFAULT_HASH_ALGORITHM,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_PATH,
    MANIFEST_SCHEMA_URL,
    BundleChecksums,
    ConflictMode,
    ContentMode,
    ExportManifest,
    FileChecksum,
    FileRecord,
    ImportError,
    ImportResult,
    PermissionRecord,
    ZoneExportOptions,
    ZoneImportOptions,
)

__all__ = [
    # Constants
    "BUNDLE_FORMAT_VERSION",
    "BUNDLE_EXTENSION",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_URL",
    "MANIFEST_SCHEMA_PATH",
    "DEFAULT_COMPRESSION_LEVEL",
    "DEFAULT_HASH_ALGORITHM",
    "BUNDLE_PATHS",
    # Enums
    "ConflictMode",
    "ContentMode",
    # Checksum models
    "FileChecksum",
    "BundleChecksums",
    # Export models
    "ZoneExportOptions",
    "ExportManifest",
    # Import models
    "ZoneImportOptions",
    "ImportResult",
    "ImportError",
    # Record models (JSONL)
    "FileRecord",
    "PermissionRecord",
    # Services
    "ZoneExportService",
    "export_zone_bundle",
    "ZoneImportService",
    "import_zone_bundle",
    # Bundle utilities
    "BundleReader",
    "validate_bundle",
    "inspect_bundle",
]
