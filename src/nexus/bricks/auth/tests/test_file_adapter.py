"""Tests for FileAdapter base class via synthetic adapter."""

from __future__ import annotations


class TestBaseImports:
    def test_base_types_importable(self) -> None:
        from nexus.bricks.auth.external_sync.base import (
            ExternalCliSyncAdapter,
            SyncedProfile,
            SyncResult,
        )

        assert SyncedProfile is not None
        assert SyncResult is not None
        assert ExternalCliSyncAdapter is not None
