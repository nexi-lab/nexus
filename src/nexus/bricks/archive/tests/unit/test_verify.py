"""Tests for archive verifier."""

import json
import sys
import tarfile
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from nexus.bricks.archive import verify as archive_verify
from nexus.bricks.archive.errors import (
    ArchiveError,
    ArchiveSignatureError,
    ArchiveVersionIncompatible,
)
from nexus.bricks.archive.verify import verify_archive
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes


def _build_signed_bundle(
    tmp_path: Path,
    *,
    signer: ArchiveSigner,
    manifest_overrides: dict | None = None,
) -> Path:
    bundle_dir = tmp_path / "b"
    bundle_dir.mkdir()
    manifest = {
        "format_version": "2.0.0",
        "nexus_version": "0.10.0",
        "bundle_id": "b",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-05-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": ""},
        "archive_kind": "full",
        "embedding_model": "bge",
        "embedding_dim": 384,
        "signer_pubkey_b64": signer.public_key_b64,
        "placeholders": [],
        "min_nexus_version": "0.0.0",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    manifest_bytes = canonical_json_bytes(manifest)
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)

    checksums = manifest["checksums"]
    assert isinstance(checksums, dict)
    payload = manifest_bytes + (checksums.get("merkle_root") or "").encode()
    sig_b64, pub_b64 = signer.sign(payload)
    sig_doc = {
        "algorithm": "ed25519",
        "signer_pubkey_b64": pub_b64,
        "signature_b64": sig_b64,
        "manifest_sha256": "0" * 64,
    }
    (bundle_dir / "signatures.json").write_text(json.dumps(sig_doc))

    out = tmp_path / "b.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for f in sorted(bundle_dir.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(bundle_dir)))
    return out


def test_verify_signed_bundle_passes(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    bundle = _build_signed_bundle(tmp_path, signer=signer)
    verify_archive(bundle, strict=True)


def test_verify_tampered_manifest_fails(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    bundle = _build_signed_bundle(tmp_path, signer=signer)

    # Tamper: extract, modify manifest, re-tar without re-signing.
    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    with tarfile.open(bundle, "r:gz") as tar:
        tar.extractall(extract_dir, filter="data")
    manifest_path = extract_dir / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["nexus_version"] = "9.9.9"
    manifest_path.write_text(json.dumps(data))
    tampered = tmp_path / "tampered.nexus"
    with tarfile.open(tampered, "w:gz") as tar:
        for f in sorted(extract_dir.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(extract_dir)))

    with pytest.raises(ArchiveSignatureError):
        verify_archive(tampered, strict=True)


def test_verify_strict_rejects_v1(tmp_path):
    bundle_dir = tmp_path / "v1"
    bundle_dir.mkdir()
    manifest = {
        "format_version": "1.0.0",
        "nexus_version": "0.9.0",
        "bundle_id": "b",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": None},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "v1.nexus"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(bundle_dir / "manifest.json", arcname="manifest.json")

    with pytest.raises(ArchiveError):
        verify_archive(out, strict=True)


def test_verify_min_version_rejected(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    bundle = _build_signed_bundle(
        tmp_path, signer=signer, manifest_overrides={"min_nexus_version": "999.0.0"}
    )
    with pytest.raises(ArchiveVersionIncompatible):
        verify_archive(bundle, strict=True)


def test_current_version_falls_back_to_source_version(monkeypatch):
    def missing_distribution(_name: str) -> str:
        raise PackageNotFoundError("nexus-ai-fs")

    monkeypatch.setattr(archive_verify, "version", missing_distribution)

    assert archive_verify._current_nexus_version() == sys.modules["nexus"].__version__
