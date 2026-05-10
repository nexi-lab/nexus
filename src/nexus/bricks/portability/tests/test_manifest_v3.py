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
