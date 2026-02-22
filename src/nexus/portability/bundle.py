"""Bundle utilities for reading and writing .nexus bundles.

This module provides utilities for working with .nexus bundle files:
- Reading and extracting bundles
- Validating bundle integrity
- Iterating over bundle contents

References:
- Issue #1162: Define .nexus bundle format
- Epic #1161: Zone Data Portability
"""

from __future__ import annotations

import json
import logging
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from nexus.portability.models import (
    BUNDLE_PATHS,
    MANIFEST_FILENAME,
    ExportManifest,
    FileRecord,
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

    def __enter__(self) -> BundleReader:
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

            manifest_data = json.loads(file_obj.read().decode("utf-8"))
            self._manifest = ExportManifest.from_dict(manifest_data)
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

        # Check required files exist
        members = {m.name for m in self._tar.getmembers()}

        if manifest.file_count > 0 and BUNDLE_PATHS["files"] not in members:
            errors.append(f"Missing required file: {BUNDLE_PATHS['files']}")

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
            logger.warning(f"Bundle missing {BUNDLE_PATHS['files']}")

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
            logger.debug(f"Bundle missing {BUNDLE_PATHS['permissions']}")

    def read_content_blob(self, content_hash: str) -> bytes | None:
        """Read a content blob from the bundle.

        Args:
            content_hash: SHA-256 hash of the content

        Returns:
            Content bytes or None if not found
        """
        if self._tar is None:
            raise RuntimeError("Bundle not open. Call open() first.")

        if len(content_hash) < 2:
            return None

        # CAS path structure: content/cas/ab/abcdef...
        prefix = content_hash[:2]
        blob_path = f"{BUNDLE_PATHS['content']}/{prefix}/{content_hash}"

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
        self._tar.extractall(output_dir)

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
