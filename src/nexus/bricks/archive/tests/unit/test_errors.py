"""Tests for archive error hierarchy."""

from nexus.bricks.archive.errors import (
    ArchiveCredentialLeakDetected,
    ArchiveEmbeddingDimMismatch,
    ArchiveError,
    ArchiveFileHashMismatch,
    ArchiveMerkleMismatch,
    ArchivePlaceholderNotInjected,
    ArchiveSignatureError,
    ArchiveTargetNotEmpty,
    ArchiveUntrustedSigner,
    ArchiveVersionIncompatible,
)


def test_all_errors_subclass_archive_error():
    classes = [
        ArchiveSignatureError,
        ArchiveMerkleMismatch,
        ArchiveFileHashMismatch,
        ArchiveVersionIncompatible,
        ArchivePlaceholderNotInjected,
        ArchiveEmbeddingDimMismatch,
        ArchiveCredentialLeakDetected,
        ArchiveUntrustedSigner,
        ArchiveTargetNotEmpty,
    ]
    for cls in classes:
        assert issubclass(cls, ArchiveError)


def test_error_codes_are_stable():
    assert ArchiveSignatureError.code == 10
    assert ArchiveMerkleMismatch.code == 11
    assert ArchiveFileHashMismatch.code == 12
    assert ArchiveVersionIncompatible.code == 13
    assert ArchivePlaceholderNotInjected.code == 20
    assert ArchiveEmbeddingDimMismatch.code == 21
    assert ArchiveCredentialLeakDetected.code == 30
    assert ArchiveUntrustedSigner.code == 40
    assert ArchiveTargetNotEmpty.code == 50


def test_signature_error_carries_context():
    err = ArchiveSignatureError("bad sig", manifest_sha="abc123")
    assert err.manifest_sha == "abc123"
    assert "bad sig" in str(err)


def test_placeholder_error_lists_missing():
    err = ArchivePlaceholderNotInjected(["HUB_TOKEN_eng", "PROVIDER_KEY_anthropic"])
    assert "HUB_TOKEN_eng" in str(err)
    assert err.missing == ["HUB_TOKEN_eng", "PROVIDER_KEY_anthropic"]


def test_target_not_empty_lists_zones():
    err = ArchiveTargetNotEmpty(["eng", "ops"])
    assert "eng" in str(err)
    assert err.existing_zones == ["eng", "ops"]
