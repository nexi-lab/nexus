"""Tests for v3 bundle manifest additions."""

import json

import pytest

from nexus.bricks.portability.models import (
    BUNDLE_FORMAT_VERSION,
    MANIFEST_SCHEMA_URL,
    ExportManifest,
)
from nexus.bricks.portability.models import (
    MANIFEST_SCHEMA_PATH as SCHEMA_PATH,
)


def test_format_version_is_v3():
    assert BUNDLE_FORMAT_VERSION == "3.0.0"


def test_manifest_schema_url_is_v3():
    assert MANIFEST_SCHEMA_URL == "https://nexus.io/schemas/manifest-v3.json"


def test_manifest_includes_mount_count():
    m = ExportManifest(source_zone_id="z1")
    m.mount_count = 5
    d = m.to_dict()
    assert d["statistics"]["mount_count"] == 5


def test_manifest_default_mount_count_is_zero():
    m = ExportManifest(source_zone_id="z1")
    assert m.mount_count == 0
    assert m.to_dict()["statistics"]["mount_count"] == 0


def test_manifest_round_trip_preserves_mount_count():
    m = ExportManifest(source_zone_id="z1")
    m.mount_count = 7
    d = m.to_dict()
    m2 = ExportManifest.from_dict(d)
    assert m2.mount_count == 7


def test_v1_bundle_loads_with_default_mount_count():
    """A v1/v2 manifest dict (no mount_count) must still load."""
    legacy_dict = {
        "format_version": "2.0.0",
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0,
            "total_size_bytes": 0,
            "content_blob_count": 0,
            "permission_count": 0,
            "embedding_count": 0,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    m = ExportManifest.from_dict(legacy_dict)
    assert m.mount_count == 0
    assert m.format_version == "2.0.0"


def test_v3_schema_file_exists_and_is_valid_json():
    text = SCHEMA_PATH.read_text()
    data = json.loads(text)
    assert data["$id"].endswith("manifest-v3.json")


def test_v3_manifest_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    m = ExportManifest(source_zone_id="z1")
    m.mount_count = 3
    jsonschema.validate(m.to_dict(), schema)


def test_v3_schema_rejects_unknown_root_field():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    bad = ExportManifest(source_zone_id="z1").to_dict()
    bad["totally_unknown_field"] = "bogus"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_v3_schema_validates_placeholders_array():
    """PlaceholderRef $def is reachable via the placeholders array."""
    jsonschema = pytest.importorskip("jsonschema")
    from nexus.bricks.portability.models import PlaceholderRef

    schema = json.loads(SCHEMA_PATH.read_text())
    m = ExportManifest(source_zone_id="z1")
    m.placeholders = [
        PlaceholderRef(name="MOUNT_m-1_ACCESS_KEY_ID", field="mounts.m-1.access_key_id"),
    ]
    jsonschema.validate(m.to_dict(), schema)


def test_v3_schema_rejects_placeholder_with_extra_field():
    """PlaceholderRef has additionalProperties: false."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    bad = ExportManifest(source_zone_id="z1").to_dict()
    bad["placeholders"] = [
        {"name": "X", "field": "a.b", "extra": "not allowed"},
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_bundle_validate_rejects_v3_manifest_with_unknown_root_key(tmp_path):
    """BundleReader.validate must reject a v3 manifest with unknown root keys.

    Issue #4083 review: ExportManifest.from_dict drops unknown fields
    silently, so without explicit JSON-Schema validation a malformed v3
    manifest passes BundleReader.validate. This test forges a bad
    manifest, packs it into a tar, and asserts validate() reports the
    schema error.
    """
    pytest.importorskip("jsonschema")
    import tarfile

    from nexus.bricks.portability.bundle import BundleReader

    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    manifest_dict = ExportManifest(source_zone_id="z1").to_dict()
    manifest_dict["totally_unknown_field"] = "this should fail validation"
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest_dict))

    out = tmp_path / "bad.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))

    with BundleReader(out) as reader:
        ok, errors = reader.validate()

    assert not ok
    assert any(
        "schema validation failed" in e.lower() or "totally_unknown_field" in e for e in errors
    ), errors


def test_bundle_validate_accepts_v2_bundle_with_v1_schema(tmp_path):
    """Round 2 reviewer finding: legacy v2 bundles emit
    $schema=manifest-v1.json. The new schema-validation step must use
    the v1 schema for v1.x/2.x bundles, not always reach for v3.
    """
    pytest.importorskip("jsonschema")
    import tarfile

    from nexus.bricks.portability.bundle import BundleReader

    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    legacy_manifest = {
        "$schema": "https://nexus.io/schemas/manifest-v1.json",
        "format_version": "2.0.0",
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0,
            "total_size_bytes": 0,
            "content_blob_count": 0,
            "permission_count": 0,
            "embedding_count": 0,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(legacy_manifest))

    out = tmp_path / "legacy.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))

    with BundleReader(out) as reader:
        ok, errors = reader.validate()

    assert ok, f"v2 bundle should pass v1-schema validation; got: {errors}"


def test_bundle_validate_rejects_unknown_future_version(tmp_path):
    """Round 3: a forged manifest with format_version='999.0.0' must be
    rejected outright, not silently routed to v3 schema validation."""
    pytest.importorskip("jsonschema")
    import tarfile

    from nexus.bricks.portability.bundle import BundleReader

    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    forged = {
        "$schema": "https://nexus.io/schemas/manifest-v3.json",
        "format_version": "999.0.0",
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0,
            "total_size_bytes": 0,
            "content_blob_count": 0,
            "permission_count": 0,
            "embedding_count": 0,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(forged))

    out = tmp_path / "forged.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))

    with BundleReader(out) as reader:
        ok, errors = reader.validate()

    assert not ok
    assert any("Unsupported manifest format_version" in e for e in errors), errors
