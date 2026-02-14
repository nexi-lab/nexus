"""Unit tests for Backend.stream_range() methods (Issue #790).

Tests cover:
- Default Backend.stream_range() (read+slice fallback)
- LocalBackend.stream_range() (seek-based efficient streaming)
- Error handling (nonexistent hash)
- Edge cases (full file, single byte, empty range)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.exceptions import NexusFileNotFoundError

# =============================================================================
# Helpers
# =============================================================================


def _create_local_backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(root_path=tmp_path)


def _write_content(backend: LocalBackend, data: bytes) -> str:
    resp = backend.write_content(data)
    return resp.unwrap()


# =============================================================================
# Default Backend.stream_range() (read + slice)
# =============================================================================


class TestDefaultBackendStreamRange:
    """Test the default stream_range() on the Backend ABC."""

    def test_stream_range_first_10_bytes(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"0123456789ABCDEF"
        content_hash = _write_content(backend, data)

        # Use the base class default impl by calling through the method
        # (LocalBackend overrides, so we test the base default explicitly)
        from nexus.backends.backend import Backend

        chunks = list(Backend.stream_range(backend, content_hash, 0, 9))
        result = b"".join(chunks)
        assert result == b"0123456789"

    def test_stream_range_middle_bytes(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"0123456789ABCDEF"
        content_hash = _write_content(backend, data)

        from nexus.backends.backend import Backend

        chunks = list(Backend.stream_range(backend, content_hash, 5, 10))
        result = b"".join(chunks)
        assert result == b"56789A"

    def test_stream_range_last_bytes(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"0123456789ABCDEF"
        content_hash = _write_content(backend, data)

        from nexus.backends.backend import Backend

        chunks = list(Backend.stream_range(backend, content_hash, 10, 15))
        result = b"".join(chunks)
        assert result == b"ABCDEF"


# =============================================================================
# LocalBackend.stream_range() (seek-based)
# =============================================================================


class TestLocalBackendStreamRange:
    def test_first_10_bytes(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"Hello, World! This is a test file for range requests."
        content_hash = _write_content(backend, data)

        chunks = list(backend.stream_range(content_hash, 0, 9))
        result = b"".join(chunks)
        assert result == b"Hello, Wor"
        assert len(result) == 10

    def test_middle_range(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        content_hash = _write_content(backend, data)

        chunks = list(backend.stream_range(content_hash, 10, 19))
        result = b"".join(chunks)
        assert result == b"KLMNOPQRST"

    def test_last_bytes(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"ABCDEFGHIJ"
        content_hash = _write_content(backend, data)

        chunks = list(backend.stream_range(content_hash, 7, 9))
        result = b"".join(chunks)
        assert result == b"HIJ"

    def test_single_byte(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"ABCDE"
        content_hash = _write_content(backend, data)

        chunks = list(backend.stream_range(content_hash, 2, 2))
        result = b"".join(chunks)
        assert result == b"C"

    def test_full_file(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"Complete file content"
        content_hash = _write_content(backend, data)

        chunks = list(backend.stream_range(content_hash, 0, len(data) - 1))
        result = b"".join(chunks)
        assert result == data

    def test_respects_chunk_size(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)
        data = b"A" * 100
        content_hash = _write_content(backend, data)

        chunks = list(backend.stream_range(content_hash, 0, 99, chunk_size=30))
        # Should have 4 chunks: 30 + 30 + 30 + 10
        assert len(chunks) == 4
        assert len(chunks[0]) == 30
        assert len(chunks[3]) == 10
        assert b"".join(chunks) == data

    def test_nonexistent_hash_raises(self, tmp_path: Path) -> None:
        backend = _create_local_backend(tmp_path)

        with pytest.raises(NexusFileNotFoundError):
            list(backend.stream_range("nonexistent_hash_abc123", 0, 10))


# =============================================================================
# AsyncLocalBackend.stream_range()
# =============================================================================


class TestAsyncLocalBackendStreamRange:
    @pytest.mark.asyncio
    async def test_first_10_bytes(self, tmp_path: Path) -> None:
        from nexus.backends.async_local import AsyncLocalBackend

        backend = AsyncLocalBackend(root_path=tmp_path)
        await backend.initialize()

        data = b"Hello, World! Async range test."
        resp = await backend.write_content(data)
        content_hash = resp.unwrap()

        chunks = []
        async for chunk in backend.stream_range(content_hash, 0, 9):
            chunks.append(chunk)
        result = b"".join(chunks)
        assert result == b"Hello, Wor"

    @pytest.mark.asyncio
    async def test_middle_range(self, tmp_path: Path) -> None:
        from nexus.backends.async_local import AsyncLocalBackend

        backend = AsyncLocalBackend(root_path=tmp_path)
        await backend.initialize()

        data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        resp = await backend.write_content(data)
        content_hash = resp.unwrap()

        chunks = []
        async for chunk in backend.stream_range(content_hash, 10, 19):
            chunks.append(chunk)
        result = b"".join(chunks)
        assert result == b"KLMNOPQRST"

    @pytest.mark.asyncio
    async def test_nonexistent_hash_raises(self, tmp_path: Path) -> None:
        from nexus.backends.async_local import AsyncLocalBackend

        backend = AsyncLocalBackend(root_path=tmp_path)
        await backend.initialize()

        with pytest.raises(NexusFileNotFoundError):
            async for _ in backend.stream_range("nonexistent_hash", 0, 10):
                pass
