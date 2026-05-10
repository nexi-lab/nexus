"""Bundle utilities for reading and writing .nexus bundles.

This module provides utilities for working with .nexus bundle files:
- Reading and extracting bundles
- Validating bundle integrity
- Iterating over bundle contents

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Zone Data Portability
"""

import json
import logging
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from nexus.bricks.portability.models import (
    BUNDLE_PATHS,
    MANIFEST_FILENAME,
    ExportManifest,
    FileRecord,
    MountRecord,
    PermissionRecord,
)

logger = logging.getLogger(__name__)


class BundleReader:
    """Reader for .nexus bundle files.

    Provides methods for reading and validating bundle contents without
    extracting the entire archive to disk.

    Example usage:
        with BundleReader("/backup/zone.nexus") as reader:
            manifest = reader.get_manifest()
            print(f"Bundle contains {manifest.file_count} files")

            for record in reader.iter_file_records():
                print(record.virtual_path)
    """

    def __init__(self, bundle_path: Path | str):
        """Initialize bundle reader.

        Args:
            bundle_path: Path to .nexus bundle file
        """
        self.bundle_path = Path(bundle_path)
        self._tar: tarfile.TarFile | None = None
        self._manifest: ExportManifest | None = None
        self._raw_manifest_dict: dict[str, Any] | None = None

    @staticmethod
    def _resolve_manifest_schema(raw_manifest: dict[str, Any]) -> Path | None:
        """Pick the JSON-Schema file matching the manifest's declared version.

        Returns None when the version is unknown or when no schema is
        applicable (e.g., a future format we don't ship a schema for).
        v1.x and v2.x bundles share manifest-v1.json (no v2 schema was
        ever shipped, and v2 bundles set ``$schema`` to manifest-v1.json
        in the wild); v3.x bundles use manifest-v3.json.
        """
        schemas_dir = Path(__file__).parent / "schemas"
        version = (raw_manifest.get("format_version") or "").strip()
        if version.startswith(("1.", "2.")):
            return schemas_dir / "manifest-v1.json"
        if version.startswith("3."):
            return schemas_dir / "manifest-v3.json"
        # Unknown/future version: best-effort fall back to the latest
        # schema we ship so reviewers see an explicit error rather than
        # silent acceptance.
        return schemas_dir / "manifest-v3.json"

    def __enter__(self) -> "BundleReader":
        """Open the bundle for reading."""
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        """Close the bundle."""
        self.close()

    def open(self) -> None:
        """Open the bundle file for reading."""
        if not self.bundle_path.exists():
            raise FileNotFoundError(f"Bundle not found: {self.bundle_path}")

        self._tar = tarfile.open(self.bundle_path, mode="r:gz")  # noqa: SIM115

    def close(self) -> None:
        """Close the bundle file."""
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    def get_manifest(self) -> ExportManifest:
        """Read and parse the bundle manifest.

        Returns:
            ExportManifest from the bundle

        Raises:
            ValueError: If manifest is missing or invalid
        """
        if self._manifest is not None:
            return self._manifest

        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        try:
            member = self._tar.getmember(MANIFEST_FILENAME)
            file_obj = self._tar.extractfile(member)
            if file_obj is None:
                raise ValueError("Could not read manifest file")

            self._raw_manifest_dict = json.loads(file_obj.read().decode("utf-8"))
            self._manifest = ExportManifest.from_dict(self._raw_manifest_dict)
            return self._manifest

        except KeyError:
            raise ValueError(f"Bundle missing {MANIFEST_FILENAME}") from None

    def validate(self) -> tuple[bool, list[str]]:
        """Validate bundle integrity.

        Checks:
        - Manifest exists and is valid
        - Required files are present
        - Checksums match (if checksums provided)

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors: list[str] = []

        if self._tar is None:
            errors.append("Bundle not open")
            return False, errors

        # Check manifest
        try:
            manifest = self.get_manifest()
            manifest_errors = manifest.validate()
            errors.extend(manifest_errors)
        except Exception as e:
            errors.append(f"Invalid manifest: {e}")
            return False, errors

        # Strict JSON-Schema validation against the version-appropriate
        # schema (Issue #4083 reviewer finding: from_dict drops unknown
        # fields, so without this step a malformed manifest with extra
        # root keys is silently accepted). Round 2 follow-up: select the
        # schema by the manifest's own format_version / $schema rather
        # than always validating against v3 — otherwise legacy v1/v2
        # bundles are rejected by the new v3 const check while
        # malformed v3 bundles slip past on slim installs.
        raw = getattr(self, "_raw_manifest_dict", None)
        if raw is not None:
            schema_path = self._resolve_manifest_schema(raw)
            if schema_path is not None:
                try:
                    import jsonschema

                    if not schema_path.exists():
                        # New v3 bundles MUST have the v3 schema available;
                        # missing it is a hard validation error so a slim
                        # wheel that dropped schemas/ doesn't silently
                        # accept malformed bundles.
                        if (raw.get("format_version") or "").startswith("3."):
                            errors.append(
                                f"Manifest schema {schema_path.name} not "
                                f"packaged with this install — cannot validate "
                                f"v3 manifest. Reinstall nexus with bundled "
                                f"schemas or import on a full install."
                            )
                    else:
                        try:
                            jsonschema.validate(raw, json.loads(schema_path.read_text()))
                        except jsonschema.ValidationError as ve:
                            errors.append(
                                f"Manifest schema validation failed "
                                f"({schema_path.name}): {ve.message}"
                            )
                except ImportError:
                    # jsonschema absent: only the v3 path is security-
                    # critical (newly-added unknown-field guard). v1/v2
                    # bundles fall back to ExportManifest.validate as
                    # before.
                    if (raw.get("format_version") or "").startswith("3."):
                        errors.append(
                            "jsonschema not installed; cannot validate v3 "
                            "manifest. Install jsonschema or import on a "
                            "full install."
                        )

        # Check required files exist
        members = {m.name for m in self._tar.getmembers()}

        if manifest.file_count > 0 and BUNDLE_PATHS["files"] not in members:
            errors.append(f"Missing required file: {BUNDLE_PATHS['files']}")

        if manifest.mount_count > 0 and BUNDLE_PATHS["mounts"] not in members:
            errors.append(
                f"Manifest claims mount_count={manifest.mount_count} but "
                f"{BUNDLE_PATHS['mounts']!r} is missing from the bundle"
            )

        # Verify checksums if provided
        if manifest.checksums.files:
            for path, checksum in manifest.checksums.files.items():
                if path not in members:
                    errors.append(f"Checksum file missing: {path}")
                    continue

                try:
                    member = self._tar.getmember(path)
                    file_obj = self._tar.extractfile(member)
                    if file_obj is not None:
                        data = file_obj.read()
                        if not checksum.verify(data):
                            errors.append(f"Checksum mismatch: {path}")
                except Exception as e:
                    errors.append(f"Error verifying {path}: {e}")

        return len(errors) == 0, errors

    def iter_file_records(self) -> Iterator[FileRecord]:
        """Iterate over file metadata records from files.jsonl.

        Yields:
            FileRecord for each file in the bundle
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        try:
            member = self._tar.getmember(BUNDLE_PATHS["files"])
            file_obj = self._tar.extractfile(member)
            if file_obj is None:
                return

            for line in file_obj:
                line_str = line.decode("utf-8").strip()
                if line_str:
                    yield FileRecord.from_jsonl(line_str)

        except KeyError:
            logger.warning("Bundle missing %s", BUNDLE_PATHS["files"])

    def iter_permission_records(self) -> Iterator[PermissionRecord]:
        """Iterate over permission records from rebac_tuples.jsonl.

        Yields:
            PermissionRecord for each permission in the bundle
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        try:
            member = self._tar.getmember(BUNDLE_PATHS["permissions"])
            file_obj = self._tar.extractfile(member)
            if file_obj is None:
                return

            for line in file_obj:
                line_str = line.decode("utf-8").strip()
                if line_str:
                    yield PermissionRecord.from_jsonl(line_str)

        except KeyError:
            logger.debug("Bundle missing %s", BUNDLE_PATHS["permissions"])

    def read_mount_records(self) -> list[MountRecord]:
        """Read mount records from mounts.jsonl.

        Returns:
            List of MountRecord objects, or empty list if mounts.jsonl is absent.
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        import json as _json

        mounts_path = BUNDLE_PATHS["mounts"]
        try:
            member = self._tar.getmember(mounts_path)
            file_obj = self._tar.extractfile(member)
            if file_obj is None:
                return []

            records: list[MountRecord] = []
            for line in file_obj:
                line_str = line.decode("utf-8").strip()
                if line_str:
                    records.append(MountRecord.from_dict(_json.loads(line_str)))
            return records

        except KeyError:
            logger.debug("Bundle missing %s", mounts_path)
            return []

    def read_content_blob(self, content_id: str) -> bytes | None:
        """Read a content blob from the bundle.

        Args:
            content_id: SHA-256 hash of the content

        Returns:
            Content bytes or None if not found
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        if len(content_id) < 2:
            return None

        # CAS path structure: content/cas/ab/abcdef...
        prefix = content_id[:2]
        blob_path = f"{BUNDLE_PATHS['content']}/{prefix}/{content_id}"

        try:
            member = self._tar.getmember(blob_path)
            file_obj = self._tar.extractfile(member)
            if file_obj is not None:
                return file_obj.read()
        except KeyError:
            pass

        return None

    def extract_to(self, output_dir: Path) -> None:
        """Extract entire bundle to a directory.

        Args:
            output_dir: Directory to extract to
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        output_dir.mkdir(parents=True, exist_ok=True)
        self._tar.extractall(output_dir, filter="data")

    def list_contents(self) -> list[str]:
        """List all files in the bundle.

        Returns:
            List of file paths in the bundle
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        return [m.name for m in self._tar.getmembers() if m.isfile()]


def validate_bundle(bundle_path: Path | str) -> tuple[bool, list[str]]:
    """Validate a .nexus bundle file.

    Args:
        bundle_path: Path to the bundle

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    try:
        with BundleReader(bundle_path) as reader:
            return reader.validate()
    except Exception as e:
        return False, [str(e)]


def inspect_bundle(bundle_path: Path | str) -> dict:
    """Get summary information about a bundle.

    Args:
        bundle_path: Path to the bundle

    Returns:
        Dictionary with bundle information
    """
    with BundleReader(bundle_path) as reader:
        manifest = reader.get_manifest()
        contents = reader.list_contents()

        return {
            "bundle_id": manifest.bundle_id,
            "format_version": manifest.format_version,
            "nexus_version": manifest.nexus_version,
            "source_zone_id": manifest.source_zone_id,
            "source_instance": manifest.source_instance,
            "export_timestamp": manifest.export_timestamp.isoformat(),
            "file_count": manifest.file_count,
            "total_size_bytes": manifest.total_size_bytes,
            "content_blob_count": manifest.content_blob_count,
            "permission_count": manifest.permission_count,
            "include_content": manifest.include_content,
            "include_permissions": manifest.include_permissions,
            "include_embeddings": manifest.include_embeddings,
            "bundle_files": len(contents),
        }
