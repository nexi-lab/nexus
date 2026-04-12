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
