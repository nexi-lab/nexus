"""Tests for embedding model/dim restore guard."""

import pytest

from nexus.bricks.archive.errors import ArchiveEmbeddingDimMismatch
from nexus.bricks.portability.import_service import _check_embedding_compat


def test_matching_model_passes():
    _check_embedding_compat(
        archive_model="bge",
        archive_dim=384,
        current_model="bge",
        current_dim=384,
        rebuild_embeddings=False,
    )


def test_dim_mismatch_raises():
    with pytest.raises(ArchiveEmbeddingDimMismatch):
        _check_embedding_compat(
            archive_model="bge",
            archive_dim=384,
            current_model="bge",
            current_dim=768,
            rebuild_embeddings=False,
        )


def test_model_mismatch_raises():
    with pytest.raises(ArchiveEmbeddingDimMismatch):
        _check_embedding_compat(
            archive_model="bge",
            archive_dim=384,
            current_model="other",
            current_dim=384,
            rebuild_embeddings=False,
        )


def test_rebuild_flag_bypasses_check():
    _check_embedding_compat(
        archive_model="bge",
        archive_dim=384,
        current_model="other",
        current_dim=768,
        rebuild_embeddings=True,
    )


def test_archive_without_embedding_metadata_passes():
    """v1 bundles (no model/dim in manifest) are not gated."""
    _check_embedding_compat(
        archive_model=None,
        archive_dim=None,
        current_model="bge",
        current_dim=384,
        rebuild_embeddings=False,
    )
