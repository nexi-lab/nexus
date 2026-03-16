"""Tests for WriteMode enum (Issue #2929)."""

import pytest

from nexus.contracts.types import WriteMode


class TestWriteMode:
    """WriteMode enum and consistency mapping."""

    def test_sync_mode(self) -> None:
        mode = WriteMode.SYNC
        assert mode.value == "sync"
        assert mode.to_metastore_consistency() == "sc"

    def test_async_mode(self) -> None:
        mode = WriteMode.ASYNC
        assert mode.value == "async"
        assert mode.to_metastore_consistency() == "ec"

    def test_from_string(self) -> None:
        assert WriteMode("sync") == WriteMode.SYNC
        assert WriteMode("async") == WriteMode.ASYNC

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            WriteMode("invalid")

    def test_values(self) -> None:
        values = [m.value for m in WriteMode]
        assert "sync" in values
        assert "async" in values
