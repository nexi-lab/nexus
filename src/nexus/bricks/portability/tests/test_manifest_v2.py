"""Tests for ExportManifest v2 fields (signing + placeholders + embedding)."""

from datetime import UTC, datetime

from nexus.bricks.portability.models import (
    BUNDLE_FORMAT_VERSION,
    ArchiveKind,
    ExportManifest,
    PlaceholderRef,
)


def test_format_version_is_v2():
    assert BUNDLE_FORMAT_VERSION == "2.0.0"


def test_manifest_round_trip_with_v2_fields():
    manifest = ExportManifest(
        format_version="2.0.0",
        nexus_version="0.10.0",
        bundle_id="b-1",
        source_instance="hub.local",
        source_zone_id="eng",
        export_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        archive_kind=ArchiveKind.FULL,
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dim=384,
        signer_pubkey_b64="cHViMQ==",
        placeholders=[
            PlaceholderRef(name="HUB_TOKEN_eng", field="federations.eng.auth_token"),
        ],
        min_nexus_version="0.10.0",
    )
    data = manifest.to_dict()
    restored = ExportManifest.from_dict(data)
    assert restored.archive_kind == ArchiveKind.FULL
    assert restored.embedding_model == "BAAI/bge-small-en-v1.5"
    assert restored.embedding_dim == 384
    assert restored.signer_pubkey_b64 == "cHViMQ=="
    assert restored.placeholders[0].name == "HUB_TOKEN_eng"
    assert restored.placeholders[0].field == "federations.eng.auth_token"
    assert restored.min_nexus_version == "0.10.0"


def test_audit_kind_carries_window():
    manifest = ExportManifest(
        format_version="2.0.0",
        nexus_version="0.10.0",
        bundle_id="b-2",
        source_instance="hub.local",
        source_zone_id="eng",
        export_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        archive_kind=ArchiveKind.AUDIT,
        activity_window_from=datetime(2026, 4, 1, tzinfo=UTC),
        activity_window_to=datetime(2026, 5, 1, tzinfo=UTC),
    )
    data = manifest.to_dict()
    restored = ExportManifest.from_dict(data)
    assert restored.archive_kind == ArchiveKind.AUDIT
    assert restored.activity_window_from == datetime(2026, 4, 1, tzinfo=UTC)


def test_v1_manifest_still_loadable():
    """Backward compat: v1 bundles read without the new fields."""
    v1_data = {
        "format_version": "1.0.0",
        "nexus_version": "0.9.0",
        "bundle_id": "b-old",
        "source_instance": "hub.local",
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
    manifest = ExportManifest.from_dict(v1_data)
    assert manifest.format_version == "1.0.0"
    assert manifest.archive_kind == ArchiveKind.FULL  # default
    assert manifest.signer_pubkey_b64 is None
    assert manifest.placeholders == []
