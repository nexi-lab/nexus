"""Tests for export-time signing wiring."""

import json
import tarfile
from pathlib import Path

import pytest

from nexus.bricks.portability.models import ZoneExportOptions
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes


@pytest.fixture
def fake_export_outputs(tmp_path):
    """Build a minimal pre-existing bundle for the signing-only path test.

    The full ZoneExportService path is exercised in integration tests; here we
    only validate that `_finalize_with_signature` writes signatures.json that
    verifies against the embedded pubkey.
    """
    from datetime import UTC, datetime

    from nexus.bricks.portability.export_service import _finalize_with_signature
    from nexus.bricks.portability.models import ArchiveKind, ExportManifest

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "metadata").mkdir()
    (bundle_dir / "metadata" / "files.jsonl").write_text("")
    manifest = ExportManifest(
        format_version="2.0.0",
        nexus_version="0.10.0",
        bundle_id="b-1",
        source_instance="hub.local",
        source_zone_id="eng",
        export_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        archive_kind=ArchiveKind.FULL,
    )
    out = tmp_path / "out.nexus"
    signer = ArchiveSigner(tmp_path / "key")
    _finalize_with_signature(bundle_dir, manifest, out, signer=signer)
    return out, signer


def test_signed_bundle_contains_signatures_json(fake_export_outputs):
    out, _signer = fake_export_outputs
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "signatures.json" in names


def test_signed_bundle_signature_verifies(fake_export_outputs):
    out, signer = fake_export_outputs
    with tarfile.open(out, "r:gz") as tar:
        sig_member = tar.getmember("signatures.json")
        sig_fh = tar.extractfile(sig_member)
        assert sig_fh is not None
        sig_data = json.loads(sig_fh.read())
        manifest_member = tar.getmember("manifest.json")
        manifest_fh = tar.extractfile(manifest_member)
        assert manifest_fh is not None
        manifest_bytes = manifest_fh.read()
    payload = canonical_json_bytes(json.loads(manifest_bytes))
    assert ArchiveSigner.verify(payload, sig_data["signature_b64"], sig_data["signer_pubkey_b64"])


def test_export_options_default_sign_on():
    opts = ZoneExportOptions(output_path=Path("/tmp/x.nexus"))
    assert opts.sign is True
    assert opts.strip_credentials is True
