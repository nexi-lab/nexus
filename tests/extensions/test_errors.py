"""Error class tests for nexus.extensions.errors."""

from nexus.extensions.errors import (
    DuplicateManifestError,
    ExtensionError,
    FactoryResolutionError,
    IndexCorruptError,
    ManifestValidationError,
    ReservedNameError,
)


def test_all_inherit_from_extension_error():
    for cls in (
        ManifestValidationError,
        DuplicateManifestError,
        ReservedNameError,
        IndexCorruptError,
        FactoryResolutionError,
    ):
        assert issubclass(cls, ExtensionError)


def test_duplicate_manifest_error_carries_sources():
    err = DuplicateManifestError(kind="connector", name="s3", sources=("entry_point", "fs_scan"))
    msg = str(err)
    assert "connector" in msg
    assert "s3" in msg
    assert "entry_point" in msg
    assert "fs_scan" in msg


def test_reserved_name_error_carries_name_and_pattern():
    err = ReservedNameError(name="_foo", pattern="leading underscore")
    msg = str(err)
    assert "_foo" in msg
    assert "leading underscore" in msg


def test_factory_resolution_error_carries_context():
    err = FactoryResolutionError(
        manifest_name="m", module="m.mod", factory="F", detail="ImportError: x"
    )
    msg = str(err)
    assert "m.mod" in msg
    assert "F" in msg
    assert "ImportError: x" in msg
