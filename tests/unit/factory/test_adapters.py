"""Tests for factory adapters — Issue #2180."""

from unittest.mock import MagicMock, patch

import pytest

from nexus.factory.adapters import _NexusFSFileReader


class TestNexusFSFileReader:
    """_NexusFSFileReader adapter tests."""

    @pytest.mark.asyncio
    async def test_read_text_bytes_decoded(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"hello world")
        reader = _NexusFSFileReader(nx)
        assert await reader.read_text("/test.txt") == "hello world"

    @pytest.mark.asyncio
    async def test_read_text_string_passthrough(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value="hello world")
        reader = _NexusFSFileReader(nx)
        assert await reader.read_text("/test.txt") == "hello world"

    def test_get_path_id_with_session(self) -> None:
        nx = MagicMock()
        reader = _NexusFSFileReader(nx)

        mock_session = MagicMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = "path-123"

        with patch("sqlalchemy.select"), patch("nexus.storage.models.FilePathModel"):
            result = reader.get_path_id("/test.txt", session=mock_session)
            assert result == "path-123"
            mock_session.execute.assert_called_once()

    def test_get_path_id_without_session(self) -> None:
        nx = MagicMock()
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value.scalar_one_or_none.return_value = "path-456"
        nx.SessionLocal.return_value = mock_session

        reader = _NexusFSFileReader(nx)

        with patch("sqlalchemy.select"), patch("nexus.storage.models.FilePathModel"):
            result = reader.get_path_id("/test.txt")
            assert result == "path-456"

    def test_get_content_hash_with_session(self) -> None:
        nx = MagicMock()
        reader = _NexusFSFileReader(nx)

        mock_session = MagicMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = "abc123"

        with patch("sqlalchemy.select"), patch("nexus.storage.models.FilePathModel"):
            result = reader.get_content_hash("/test.txt", session=mock_session)
            assert result == "abc123"

    @pytest.mark.asyncio
    async def test_list_files_items_attribute(self) -> None:
        nx = MagicMock()
        mock_result = MagicMock()
        mock_result.items = ["/a.txt", "/b.txt"]
        nx.sys_readdir = MagicMock(return_value=mock_result)
        reader = _NexusFSFileReader(nx)
        assert await reader.list_files("/") == ["/a.txt", "/b.txt"]

    @pytest.mark.asyncio
    async def test_list_files_list_fallback(self) -> None:
        nx = MagicMock()
        nx.sys_readdir = MagicMock(return_value=["/a.txt", "/b.txt"])
        reader = _NexusFSFileReader(nx)
        result = await reader.list_files("/")
        assert len(result) >= 2

    # ------------------------------------------------------------------
    # PR #3789: parse_fn decoding for parseable binaries (Issue #3757).
    # Search indexing reads via read_text; without parse_fn, PDFs index as
    # utf-8 garbage.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_read_text_uses_cached_parsed_text_for_pdf(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 binary-bytes")
        nx.metadata.get_file_metadata = MagicMock(return_value="cached markdown")
        # parse_fn must NOT be invoked when metastore has the cache.
        parse_fn = MagicMock()
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/doc.pdf") == "cached markdown"
        parse_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_text_invokes_parse_fn_when_no_cache(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 binary-bytes")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        nx.metadata.set_file_metadata = MagicMock()
        parse_fn = MagicMock(return_value=b"# Title\n\nBody text.")
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        result = await reader.read_text("/doc.pdf")
        assert result == "# Title\n\nBody text."
        parse_fn.assert_called_once_with(b"%PDF-1.4 binary-bytes", "/doc.pdf")
        # Parsed text should be written back to the metastore cache.
        cache_calls = [
            c for c in nx.metadata.set_file_metadata.call_args_list if c.args[1] == "parsed_text"
        ]
        assert len(cache_calls) == 1
        assert cache_calls[0].args[2] == "# Title\n\nBody text."

    @pytest.mark.asyncio
    async def test_read_text_falls_back_to_raw_when_parse_returns_none(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 unparseable")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        parse_fn = MagicMock(return_value=None)
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        # errors="ignore" means we get best-effort garbage — acceptable
        # fallback; the point is read_text must not crash.
        result = await reader.read_text("/doc.pdf")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_read_text_no_parse_fn_falls_back_to_raw_for_pdf(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"hello")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        reader = _NexusFSFileReader(nx, parse_fn=None)

        assert await reader.read_text("/doc.pdf") == "hello"

    @pytest.mark.asyncio
    async def test_read_text_non_parseable_extension_skips_parse_fn(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"plain text")
        parse_fn = MagicMock()
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/notes.txt") == "plain text"
        parse_fn.assert_not_called()
        nx.metadata.get_file_metadata.assert_not_called()
