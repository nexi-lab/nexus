"""Tenant Data Portability module for .nexus bundle export/import.

This module provides complete tenant data portability through the .nexus
bundle format, enabling:

- Export tenant data (files, metadata, permissions, embeddings) as portable bundle
- Import bundles into another Nexus instance with tenant ID remapping
- Cross-tenant migration (Company A → Company B)
- GDPR Article 20 compliance (Right to Data Portability)

Example usage:

    # Export tenant data
    from nexus.portability import TenantExportOptions, ExportManifest

    options = TenantExportOptions(
        output_path="/backup/tenant.nexus",
        include_content=True,
        include_permissions=True,
    )
    # Export service creates the bundle

    # Import tenant data
    from nexus.portability import TenantImportOptions, ImportResult

    options = TenantImportOptions(
        bundle_path="/backup/tenant.nexus",
        target_tenant_id="new-tenant",
        conflict_mode=ConflictMode.SKIP,
    )
    # Import service processes the bundle

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Tenant Data Portability
"""

from nexus.portability.bundle import (
    BundleReader,
    inspect_bundle,
    validate_bundle,
)
from nexus.portability.export_service import (
    TenantExportService,
    export_tenant_bundle,
)
from nexus.portability.import_service import (
    TenantImportService,
    import_tenant_bundle,
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
    TenantExportOptions,
    TenantImportOptions,
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
    "TenantExportOptions",
    "ExportManifest",
    # Import models
    "TenantImportOptions",
    "ImportResult",
    "ImportError",
    # Record models (JSONL)
    "FileRecord",
    "PermissionRecord",
    # Services
    "TenantExportService",
    "export_tenant_bundle",
    "TenantImportService",
    "import_tenant_bundle",
    # Bundle utilities
    "BundleReader",
    "validate_bundle",
    "inspect_bundle",
]
