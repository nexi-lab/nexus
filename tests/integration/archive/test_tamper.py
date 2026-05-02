"""Tamper detection during verify (#3793, Task 22).

Mutates the manifest.json inside a signed archive and asserts that
verify_archive(strict=True) raises ArchiveSignatureError.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from nexus.bricks.archive.errors import ArchiveSignatureError
from nexus.bricks.archive.verify import verify_archive


def _build_signed_bundle(tmp_path: Path) -> Path:
    """Create a minimal signed archive and return its path."""
    from nexus.bricks.portability.export_service import ZoneExportService
    from nexus.bricks.portability.models import ZoneExportOptions
    from tests.integration.archive.helpers import boot_lightweight_nexus

    db_path = tmp_path / "nexus.db"
    fs = boot_lightweight_nexus(db_path=db_path)
    fs.write("/eng/doc.txt", b"test content", context=fs._init_cred)

    key_path = tmp_path / "key"
    out = tmp_path / "orig.nexus"
    options = ZoneExportOptions(
        output_path=out,
        include_content=False,
        sign=True,
        strip_credentials=False,
        signing_key_path=key_path,
    )
    ZoneExportService(fs).export_zone("root", options)
    fs.shutdown()
    return out


def _retar_with_modified_manifest(orig: Path, output: Path, mutator) -> None:
    """Extract bundle, mutate manifest.json via mutator, repack."""
    extract = output.parent / "extracted"
    extract.mkdir(exist_ok=True)
    with tarfile.open(orig, "r:gz") as tar:
        tar.extractall(extract, filter="data")
    manifest_path = extract / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    mutator(manifest)
    manifest_path.write_text(json.dumps(manifest))
    with tarfile.open(output, "w:gz") as tar:
        for f in sorted(extract.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(extract)))


def test_original_signed_bundle_passes(tmp_path):
    """Sanity: unmodified signed bundle should verify cleanly."""
    bundle = _build_signed_bundle(tmp_path)
    verify_archive(bundle, strict=True)  # must not raise


def test_tampered_nexus_version_rejected(tmp_path):
    """Mutating nexus_version invalidates the ed25519 signature."""
    orig = _build_signed_bundle(tmp_path)
    tampered = tmp_path / "tampered.nexus"
    _retar_with_modified_manifest(orig, tampered, lambda m: m.update({"nexus_version": "9.9.9"}))
    with pytest.raises(ArchiveSignatureError):
        verify_archive(tampered, strict=True)


def test_tampered_file_count_rejected(tmp_path):
    """Mutating statistics.file_count invalidates the ed25519 signature."""
    orig = _build_signed_bundle(tmp_path)
    tampered = tmp_path / "tampered.nexus"

    def _mutate(m):
        stats = m.setdefault("statistics", {})
        stats["file_count"] = stats.get("file_count", 0) + 99

    _retar_with_modified_manifest(orig, tampered, _mutate)
    with pytest.raises(ArchiveSignatureError):
        verify_archive(tampered, strict=True)


def test_tampered_source_zone_rejected(tmp_path):
    """Mutating source_zone_id invalidates the ed25519 signature."""
    orig = _build_signed_bundle(tmp_path)
    tampered = tmp_path / "tampered.nexus"
    _retar_with_modified_manifest(orig, tampered, lambda m: m.update({"source_zone_id": "evil"}))
    with pytest.raises(ArchiveSignatureError):
        verify_archive(tampered, strict=True)


def test_unsigned_bundle_strict_mode_raises(tmp_path):
    """An unsigned bundle raises ArchiveSignatureError when strict=True."""
    from nexus.bricks.portability.export_service import ZoneExportService
    from nexus.bricks.portability.models import ZoneExportOptions
    from tests.integration.archive.helpers import boot_lightweight_nexus

    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    fs.write("/eng/doc.txt", b"test", context=fs._init_cred)
    out = tmp_path / "unsigned.nexus"
    options = ZoneExportOptions(
        output_path=out,
        include_content=False,
        sign=False,
        strip_credentials=False,
    )
    ZoneExportService(fs).export_zone("root", options)
    fs.shutdown()

    with pytest.raises(ArchiveSignatureError):
        verify_archive(out, strict=True)
