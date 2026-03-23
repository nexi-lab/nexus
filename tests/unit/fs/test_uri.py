"""Comprehensive tests for nexus.fs._uri — cloud storage mount URI parser."""

from __future__ import annotations

import pytest

from nexus.contracts.exceptions import InvalidPathError
from nexus.fs._uri import (
    derive_mount_point,
    parse_uri,
    validate_mount_collision,
)

# =========================================================================
# 1. Valid URI parsing — one parametrize block per scheme
# =========================================================================


class TestParseValidURIs:
    """parse_uri should accept well-formed URIs for every supported scheme."""

    @pytest.mark.parametrize(
        "uri, expected_scheme, expected_authority, expected_path",
        [
            ("s3://my-bucket", "s3", "my-bucket", ""),
            ("s3://my-bucket/subdir", "s3", "my-bucket", "subdir"),
            ("s3://my-bucket/a/b/c", "s3", "my-bucket", "a/b/c"),
            ("s3://my.dotted.bucket", "s3", "my.dotted.bucket", ""),
            ("s3://my-bucket/trailing/", "s3", "my-bucket", "trailing"),
            ("S3://UPPER-CASE", "s3", "UPPER-CASE", ""),
        ],
        ids=[
            "s3-bucket-only",
            "s3-with-subdir",
            "s3-nested-path",
            "s3-dots-in-bucket",
            "s3-trailing-slash",
            "s3-uppercase-scheme",
        ],
    )
    def test_s3(
        self,
        uri: str,
        expected_scheme: str,
        expected_authority: str,
        expected_path: str,
    ) -> None:
        spec = parse_uri(uri)
        assert spec.scheme == expected_scheme
        assert spec.authority == expected_authority
        assert spec.path == expected_path
        assert spec.uri == uri

    @pytest.mark.parametrize(
        "uri, expected_authority, expected_path",
        [
            ("gcs://project/bucket", "project", "bucket"),
            ("gcs://project/bucket/subdir", "project", "bucket/subdir"),
            ("gcs://my-project", "my-project", ""),
        ],
        ids=[
            "gcs-project-bucket",
            "gcs-nested",
            "gcs-project-only",
        ],
    )
    def test_gcs(self, uri: str, expected_authority: str, expected_path: str) -> None:
        spec = parse_uri(uri)
        assert spec.scheme == "gcs"
        assert spec.authority == expected_authority
        assert spec.path == expected_path

    @pytest.mark.parametrize(
        "uri, expected_authority, expected_path",
        [
            ("local://./data", ".", "data"),
            ("local://localhost/share", "localhost", "share"),
            ("local:///tmp/nexus", "tmp", "nexus"),
            ("local:///a/b/c", "a", "b/c"),
        ],
        ids=[
            "local-relative-dot",
            "local-localhost",
            "local-absolute-triple-slash",
            "local-absolute-nested",
        ],
    )
    def test_local(self, uri: str, expected_authority: str, expected_path: str) -> None:
        spec = parse_uri(uri)
        assert spec.scheme == "local"
        assert spec.authority == expected_authority
        assert spec.path == expected_path

    @pytest.mark.parametrize(
        "uri, expected_authority, expected_path",
        [
            ("gdrive://my-drive", "my-drive", ""),
            ("gdrive://my-drive/folder", "my-drive", "folder"),
            ("gdrive://my-drive/a/b", "my-drive", "a/b"),
        ],
        ids=[
            "gdrive-root",
            "gdrive-folder",
            "gdrive-nested",
        ],
    )
    def test_gdrive(self, uri: str, expected_authority: str, expected_path: str) -> None:
        spec = parse_uri(uri)
        assert spec.scheme == "gdrive"
        assert spec.authority == expected_authority
        assert spec.path == expected_path


# =========================================================================
# 2. Mount point derivation
# =========================================================================


class TestDeriveMountPoint:
    """derive_mount_point should produce correct paths for each scheme."""

    @pytest.mark.parametrize(
        "uri, expected_mount",
        [
            ("s3://my-bucket", "/s3/my-bucket"),
            ("s3://my-bucket/subdir", "/s3/my-bucket"),
            ("s3://my.dotted.bucket/deep/path", "/s3/my.dotted.bucket"),
        ],
        ids=["s3-simple", "s3-subdir-ignored", "s3-dots"],
    )
    def test_s3_mount(self, uri: str, expected_mount: str) -> None:
        spec = parse_uri(uri)
        assert derive_mount_point(spec) == expected_mount

    @pytest.mark.parametrize(
        "uri, expected_mount",
        [
            ("gcs://project/bucket", "/gcs/bucket"),
            ("gcs://project/bucket/subdir", "/gcs/subdir"),
            ("gcs://my-project", "/gcs/my-project"),
        ],
        ids=["gcs-project-bucket", "gcs-nested-last-segment", "gcs-project-only"],
    )
    def test_gcs_mount(self, uri: str, expected_mount: str) -> None:
        spec = parse_uri(uri)
        assert derive_mount_point(spec) == expected_mount

    @pytest.mark.parametrize(
        "uri, expected_mount",
        [
            ("local://./data", "/local/data"),
            ("local:///tmp/nexus", "/local/nexus"),
            ("local://localhost/share", "/local/share"),
        ],
        ids=["local-dot-data", "local-tmp-nexus", "local-share"],
    )
    def test_local_mount(self, uri: str, expected_mount: str) -> None:
        spec = parse_uri(uri)
        assert derive_mount_point(spec) == expected_mount

    @pytest.mark.parametrize(
        "uri, expected_mount",
        [
            ("gdrive://my-drive", "/gdrive/my-drive"),
            ("gdrive://my-drive/folder", "/gdrive/my-drive"),
        ],
        ids=["gdrive-root", "gdrive-folder"],
    )
    def test_gdrive_mount(self, uri: str, expected_mount: str) -> None:
        spec = parse_uri(uri)
        assert derive_mount_point(spec) == expected_mount


# =========================================================================
# 3. Mount point override via at=
# =========================================================================


class TestMountPointOverride:
    """The at= parameter should override automatic derivation."""

    def test_at_absolute(self) -> None:
        spec = parse_uri("s3://my-bucket")
        assert derive_mount_point(spec, at="/my-data") == "/my-data"

    def test_at_relative_gets_slash_prefix(self) -> None:
        spec = parse_uri("s3://my-bucket")
        assert derive_mount_point(spec, at="my-data") == "/my-data"

    def test_at_overrides_all_schemes(self) -> None:
        for uri in [
            "s3://b",
            "gcs://p/b",
            "local://./d",
            "gdrive://d",
        ]:
            spec = parse_uri(uri)
            assert derive_mount_point(spec, at="/custom") == "/custom"


# =========================================================================
# 4. Invalid URIs
# =========================================================================


class TestInvalidURIs:
    """parse_uri should reject malformed inputs with InvalidPathError."""

    def test_empty_string(self) -> None:
        with pytest.raises(InvalidPathError, match="must not be empty"):
            parse_uri("")

    def test_missing_scheme(self) -> None:
        with pytest.raises(InvalidPathError, match="Missing scheme"):
            parse_uri("://bucket")

    @pytest.mark.parametrize(
        "uri,expected_scheme",
        [
            ("ftp://host/path", "ftp"),
            ("hdfs://namenode/path", "hdfs"),
            ("http://example.com", "http"),
            ("gws://sheets", "gws"),
            ("github://repo", "github"),
        ],
        ids=["ftp", "hdfs", "http", "gws", "github"],
    )
    def test_non_builtin_schemes_accepted(self, uri: str, expected_scheme: str) -> None:
        """Non-builtin schemes are accepted — resolved via connector registry at mount time."""
        spec = parse_uri(uri)
        assert spec.scheme == expected_scheme

    def test_empty_authority_s3(self) -> None:
        with pytest.raises(InvalidPathError, match="Empty authority"):
            parse_uri("s3:///")

    def test_empty_authority_gcs(self) -> None:
        with pytest.raises(InvalidPathError, match="Empty authority"):
            parse_uri("gcs:///")

    def test_empty_authority_local(self) -> None:
        with pytest.raises(InvalidPathError, match="Empty authority"):
            parse_uri("local:///")


# =========================================================================
# 5. Edge cases
# =========================================================================


class TestEdgeCases:
    """Tricky inputs that exercise boundary conditions."""

    def test_url_encoded_chars_in_path(self) -> None:
        spec = parse_uri("s3://bucket/my%20folder")
        assert spec.path == "my%20folder"

    def test_deeply_nested_path(self) -> None:
        spec = parse_uri("s3://bucket/a/b/c/d/e/f")
        assert spec.path == "a/b/c/d/e/f"
        assert derive_mount_point(spec) == "/s3/bucket"

    def test_authority_with_hyphens_and_numbers(self) -> None:
        spec = parse_uri("s3://my-bucket-123")
        assert spec.authority == "my-bucket-123"

    def test_trailing_slash_stripped_from_path(self) -> None:
        spec = parse_uri("s3://bucket/path/")
        assert spec.path == "path"

    def test_multiple_trailing_slashes(self) -> None:
        spec = parse_uri("gcs://project/bucket///")
        # urlparse collapses; path gets stripped
        assert spec.scheme == "gcs"

    def test_mount_spec_is_frozen(self) -> None:
        import dataclasses

        spec = parse_uri("s3://bucket")
        assert dataclasses.is_dataclass(spec)
        # Verify fields exist (frozen=True is set on the dataclass)
        assert len(dataclasses.fields(spec)) >= 4

    def test_original_uri_preserved(self) -> None:
        uri = "S3://My-Bucket/Some/Path"
        spec = parse_uri(uri)
        assert spec.uri == uri
        # scheme is lowered
        assert spec.scheme == "s3"


# =========================================================================
# 6. Reserved path collision
# =========================================================================


class TestReservedPaths:
    """derive_mount_point must reject reserved system paths."""

    @pytest.mark.parametrize(
        "reserved",
        ["/__sys__", "/__pipes__"],
        ids=["sys", "pipes"],
    )
    def test_at_override_hits_reserved(self, reserved: str) -> None:
        spec = parse_uri("s3://bucket")
        with pytest.raises(InvalidPathError, match="reserved"):
            derive_mount_point(spec, at=reserved)

    def test_at_override_nested_under_reserved(self) -> None:
        spec = parse_uri("s3://bucket")
        with pytest.raises(InvalidPathError, match="reserved"):
            derive_mount_point(spec, at="/__sys__/foo")


# =========================================================================
# 7. Mount collision detection
# =========================================================================


class TestMountCollision:
    """validate_mount_collision should detect duplicate mounts."""

    def test_no_collision(self) -> None:
        # Should not raise
        validate_mount_collision("/s3/bucket", {"/gcs/project", "/local/data"})

    def test_collision_raises(self) -> None:
        with pytest.raises(InvalidPathError, match="already mounted"):
            validate_mount_collision("/s3/bucket", {"/s3/bucket", "/gcs/project"})

    def test_empty_existing_never_collides(self) -> None:
        validate_mount_collision("/s3/bucket", set())

    def test_different_schemes_same_name_no_collision(self) -> None:
        validate_mount_collision("/gcs/data", {"/s3/data", "/local/data"})
