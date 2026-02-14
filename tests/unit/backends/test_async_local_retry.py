"""Tests for async_local.py tenacity retry behavior (Issue #1300).

Validates that _read_metadata and _write_metadata use async retry
with tenacity, freeing thread pool slots during backoff sleep.

Does NOT depend on pytest-asyncio — uses explicit event loop helper.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.backends.async_local import AsyncLocalBackend
from nexus.core.exceptions import BackendError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def backend(tmp_path: Path) -> AsyncLocalBackend:
    """Create a temporary async local backend for testing."""
    b = AsyncLocalBackend(root_path=tmp_path / "backend")
    _run_async(b.initialize())
    return b


# === _read_metadata tests ===


class TestReadMetadataRetry:
    """Tests for _read_metadata tenacity retry behavior."""

    def test_read_returns_default_when_file_missing(self, backend: AsyncLocalBackend):
        """Missing metadata file should return default dict, no retry needed."""
        result = _run_async(backend._read_metadata("deadbeef1234"))
        assert result == {"ref_count": 0, "size": 0}

    def test_read_succeeds_on_valid_metadata(self, backend: AsyncLocalBackend):
        """Valid metadata file should be read without retries."""
        content_hash = "ab" * 32
        meta_path = backend._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({"ref_count": 3, "size": 1024}))

        result = _run_async(backend._read_metadata(content_hash))
        assert result == {"ref_count": 3, "size": 1024}

    def test_read_retries_on_json_decode_error(self, backend: AsyncLocalBackend):
        """JSONDecodeError should trigger retry, succeed when file becomes valid."""
        content_hash = "ab" * 32
        meta_path = backend._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("{corrupt")

        call_count = 0
        original_read_text = Path.read_text

        def _patched_read_text(self_path, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Fix the file on second read
                return json.dumps({"ref_count": 1, "size": 100})
            return original_read_text(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", _patched_read_text):
            result = _run_async(backend._read_metadata(content_hash))

        assert result == {"ref_count": 1, "size": 100}
        assert call_count >= 2

    def test_read_retries_on_os_error(self, backend: AsyncLocalBackend):
        """OSError should trigger retry."""
        content_hash = "ab" * 32
        meta_path = backend._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({"ref_count": 1, "size": 50}))

        call_count = 0
        original_read_text = Path.read_text

        def _patched_read_text(self_path, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("temporary I/O failure")
            return original_read_text(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", _patched_read_text):
            result = _run_async(backend._read_metadata(content_hash))

        assert result == {"ref_count": 1, "size": 50}
        assert call_count == 2

    def test_read_raises_backend_error_after_all_retries(self, backend: AsyncLocalBackend):
        """After 10 failed attempts, BackendError should be raised."""
        content_hash = "ab" * 32
        meta_path = backend._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("{always-corrupt")

        with pytest.raises(BackendError, match="Failed to read metadata"):
            _run_async(backend._read_metadata(content_hash))

    def test_read_retry_uses_async_sleep(self, backend: AsyncLocalBackend):
        """Verify tenacity uses asyncio.sleep (not time.sleep) during backoff."""
        content_hash = "ab" * 32
        meta_path = backend._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({"ref_count": 1, "size": 10}))

        call_count = 0
        original_read_text = Path.read_text

        def _patched_read_text(self_path, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise OSError("temporary failure")
            return original_read_text(self_path, *args, **kwargs)

        sleep_calls: list[float] = []

        async def _track_sleep(seconds):
            sleep_calls.append(seconds)

        with (
            patch.object(Path, "read_text", _patched_read_text),
            patch("asyncio.sleep", _track_sleep),
        ):
            result = _run_async(backend._read_metadata(content_hash))

        assert result == {"ref_count": 1, "size": 10}
        assert len(sleep_calls) >= 1, "Expected asyncio.sleep calls for retry backoff"


# === _write_metadata tests ===


class TestWriteMetadataRetry:
    """Tests for _write_metadata tenacity retry behavior."""

    def test_write_succeeds_on_first_attempt(self, backend: AsyncLocalBackend):
        """Normal write should succeed without retries."""
        content_hash = "cd" * 32
        metadata = {"ref_count": 1, "size": 512}

        _run_async(backend._write_metadata(content_hash, metadata))

        meta_path = backend._get_meta_path(content_hash)
        assert meta_path.exists()
        stored = json.loads(meta_path.read_text())
        assert stored == metadata

    def test_write_retries_on_permission_error(self, backend: AsyncLocalBackend):
        """PermissionError should trigger retry, succeed when lock released."""
        content_hash = "cd" * 32
        metadata = {"ref_count": 2, "size": 256}

        call_count = 0
        _original_replace = os.replace

        def _patched_replace(src, dst):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise PermissionError("file locked by antivirus")
            return _original_replace(src, dst)

        with patch("os.replace", _patched_replace):
            _run_async(backend._write_metadata(content_hash, metadata))

        assert call_count == 3
        meta_path = backend._get_meta_path(content_hash)
        stored = json.loads(meta_path.read_text())
        assert stored == metadata

    def test_write_fails_immediately_on_non_permission_os_error(self, backend: AsyncLocalBackend):
        """Non-PermissionError OSError should NOT be retried — fail immediately."""
        content_hash = "cd" * 32
        metadata = {"ref_count": 1, "size": 100}

        call_count = 0

        def _patched_replace(src, dst):
            nonlocal call_count
            call_count += 1
            raise OSError("disk full")

        with (
            patch("os.replace", _patched_replace),
            pytest.raises(BackendError, match="Failed to write metadata"),
        ):
            _run_async(backend._write_metadata(content_hash, metadata))

        assert call_count == 1  # No retries for generic OSError

    def test_write_raises_backend_error_after_all_retries(self, backend: AsyncLocalBackend):
        """After 10 PermissionError retries, BackendError should be raised."""
        content_hash = "cd" * 32
        metadata = {"ref_count": 1, "size": 100}

        def _always_fail(src, dst):
            raise PermissionError("permanently locked")

        with (
            patch("os.replace", _always_fail),
            pytest.raises(BackendError, match="Failed to write metadata"),
        ):
            _run_async(backend._write_metadata(content_hash, metadata))

    def test_write_cleans_up_temp_file_on_failure(self, backend: AsyncLocalBackend):
        """Temp files should be cleaned up when write fails."""
        content_hash = "cd" * 32
        meta_path = backend._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"ref_count": 1, "size": 100}

        def _always_fail(src, dst):
            raise OSError("disk full")

        with (
            patch("os.replace", _always_fail),
            pytest.raises(BackendError),
        ):
            _run_async(backend._write_metadata(content_hash, metadata))

        tmp_files = list(meta_path.parent.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Temp files not cleaned up: {tmp_files}"

    def test_write_atomic_replace(self, backend: AsyncLocalBackend):
        """Write should use atomic temp-file + os.replace pattern."""
        content_hash = "cd" * 32
        initial = {"ref_count": 1, "size": 100}
        updated = {"ref_count": 2, "size": 200}

        _run_async(backend._write_metadata(content_hash, initial))
        meta_path = backend._get_meta_path(content_hash)
        assert json.loads(meta_path.read_text()) == initial

        _run_async(backend._write_metadata(content_hash, updated))
        assert json.loads(meta_path.read_text()) == updated


# === Integration: read + write round-trip ===


class TestMetadataRoundTrip:
    """Integration tests for read/write metadata cycle."""

    def test_write_then_read(self, backend: AsyncLocalBackend):
        """Write metadata, then read it back."""
        content_hash = "ef" * 32
        metadata = {"ref_count": 5, "size": 2048, "custom_field": "value"}

        _run_async(backend._write_metadata(content_hash, metadata))
        result = _run_async(backend._read_metadata(content_hash))

        assert result == metadata

    def test_concurrent_reads(self, backend: AsyncLocalBackend):
        """Multiple concurrent reads should all succeed."""
        content_hash = "ef" * 32
        metadata = {"ref_count": 3, "size": 512}
        _run_async(backend._write_metadata(content_hash, metadata))

        async def _read_many():
            return await asyncio.gather(*[backend._read_metadata(content_hash) for _ in range(10)])

        results = _run_async(_read_many())

        for result in results:
            assert result == metadata
