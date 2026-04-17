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
        import hashlib

        raw = b"%PDF-1.4 binary-bytes"
        raw_hash = hashlib.sha256(raw).hexdigest()

        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=raw)
        # Metastore returns cached text AND a matching raw-hash — both must
        # line up for the cache to be trusted.
        nx.metadata.get_file_metadata = MagicMock(
            side_effect=lambda _p, key: {
                "parsed_text": "cached markdown",
                "parsed_text_hash": raw_hash,
            }.get(key)
        )
        # parse_fn must NOT be invoked when metastore has the cache.
        parse_fn = MagicMock()
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/doc.pdf") == "cached markdown"
        parse_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_text_reparses_when_raw_hash_diverges_from_cache(self) -> None:
        # Content-hash keyed cache: a cached entry whose ``parsed_text_hash``
        # does not match the hash of the raw bytes we just read is treated
        # as stale and ignored.  This protects against cross-zone
        # contamination (``/report.pdf`` in two different zones sharing
        # path-keyed metadata) and against a rewrite-before-reindex race
        # where the cached text belongs to the previous revision.
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 FRESH bytes")
        nx.metadata.get_file_metadata = MagicMock(
            side_effect=lambda _p, key: {
                "parsed_text": "STALE cached markdown",
                "parsed_text_hash": "hash-of-different-bytes",
            }.get(key)
        )
        nx.metadata.set_file_metadata = MagicMock()
        parse_fn = MagicMock(return_value=b"fresh markdown")
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/doc.pdf") == "fresh markdown"
        parse_fn.assert_called_once_with(b"%PDF-1.4 FRESH bytes", "/doc.pdf")
        # The refreshed parse must be cached under BOTH keys so subsequent
        # reads hit the fast path with the new hash.
        keys_written = {c.args[1] for c in nx.metadata.set_file_metadata.call_args_list}
        assert "parsed_text" in keys_written
        assert "parsed_text_hash" in keys_written

    @pytest.mark.asyncio
    async def test_read_text_reparses_when_cache_missing_hash_companion(self) -> None:
        # Legacy cache entries written before the hash key existed have
        # ``parsed_text`` but no ``parsed_text_hash``.  We must NOT serve
        # them as-is (we can't prove they match the current bytes); treat
        # them as stale and re-parse.
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 bytes")
        nx.metadata.get_file_metadata = MagicMock(
            side_effect=lambda _p, key: {
                "parsed_text": "legacy cached markdown",
                # No parsed_text_hash for this path.
            }.get(key)
        )
        nx.metadata.set_file_metadata = MagicMock()
        parse_fn = MagicMock(return_value=b"fresh markdown")
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/doc.pdf") == "fresh markdown"
        parse_fn.assert_called_once()

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
    async def test_read_text_fails_closed_when_parse_returns_none(self) -> None:
        # Fail-closed: parseable binary with broken parser must NOT emit
        # raw utf-8 garbage — the indexer would waste embedding budget on
        # it and pollute search results.  Returning "" lets the daemon
        # skip the file.
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 unparseable")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        parse_fn = MagicMock(return_value=None)
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/doc.pdf") == ""

    @pytest.mark.asyncio
    async def test_read_text_fails_closed_when_parse_fn_missing(self) -> None:
        # Same fail-closed semantics when no parser is wired at all.
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"hello")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        reader = _NexusFSFileReader(nx, parse_fn=None)

        assert await reader.read_text("/doc.pdf") == ""

    @pytest.mark.asyncio
    async def test_read_text_fails_closed_when_parse_fn_raises(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        parse_fn = MagicMock(side_effect=RuntimeError("boom"))
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/doc.pdf") == ""

    @pytest.mark.asyncio
    async def test_read_text_non_parseable_extension_skips_parse_fn(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"plain text")
        parse_fn = MagicMock()
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        assert await reader.read_text("/notes.txt") == "plain text"
        parse_fn.assert_not_called()
        nx.metadata.get_file_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_text_strips_nul_bytes_from_parsed_output(self) -> None:
        # PDF parsers can emit embedded NUL from stream artifacts; Postgres
        # rejects them in text columns and the indexer write transaction would
        # be rolled back otherwise (SQLSTATE 22021).
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        parse_fn = MagicMock(return_value=b"Clean\x00 Dirty\x00\x00Text")
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        result = await reader.read_text("/doc.pdf")
        assert "\x00" not in result
        assert result == "Clean DirtyText"

    @pytest.mark.asyncio
    async def test_read_text_strips_nul_bytes_before_caching(self) -> None:
        # Regression for the adversarial-review finding: the metastore cache
        # used to receive the un-sanitized string, so the next reindex that
        # took the cached fast path re-hit the Postgres NUL rejection.
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        nx.metadata.set_file_metadata = MagicMock()
        parse_fn = MagicMock(return_value=b"A\x00B\x00C")
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        await reader.read_text("/doc.pdf")
        cache_calls = [
            c for c in nx.metadata.set_file_metadata.call_args_list if c.args[1] == "parsed_text"
        ]
        assert len(cache_calls) == 1
        assert "\x00" not in cache_calls[0].args[2]
        assert cache_calls[0].args[2] == "ABC"

    @pytest.mark.asyncio
    async def test_read_text_sanitizes_poisoned_cache_on_read(self) -> None:
        # Defense-in-depth: cache entries written with NULs before the
        # write-path sanitizer existed must still be scrubbed on the
        # cached-read path when their hash matches the current raw bytes.
        import hashlib

        raw = b"%PDF-1.4"
        raw_hash = hashlib.sha256(raw).hexdigest()

        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=raw)
        nx.metadata.get_file_metadata = MagicMock(
            side_effect=lambda _p, key: {
                "parsed_text": "cached\x00 string",
                "parsed_text_hash": raw_hash,
            }.get(key)
        )
        parse_fn = MagicMock()
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        result = await reader.read_text("/doc.pdf")
        assert result == "cached string"
        parse_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_text_strips_nul_bytes_from_raw_fallback(self) -> None:
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"raw\x00bytes")
        reader = _NexusFSFileReader(nx, parse_fn=None)

        # Non-parseable extension → raw decode path must also strip NULs.
        assert await reader.read_text("/notes.txt") == "rawbytes"

    def test_get_searchable_text_sanitizes_poisoned_cache(self) -> None:
        # IndexingService._read_content prefers get_searchable_text over
        # read_text; a poisoned metastore entry (written by
        # ContentParserEngine or pre-sanitizer adapter versions) must be
        # scrubbed on the way out or the Postgres write still rolls back.
        nx = MagicMock()
        nx.metadata.get_searchable_text = MagicMock(return_value="hello\x00world")
        reader = _NexusFSFileReader(nx)

        assert reader.get_searchable_text("/doc.pdf") == "helloworld"

    def test_get_searchable_text_passes_through_none(self) -> None:
        nx = MagicMock()
        nx.metadata.get_searchable_text = MagicMock(return_value=None)
        reader = _NexusFSFileReader(nx)

        assert reader.get_searchable_text("/missing.pdf") is None

    @pytest.mark.asyncio
    async def test_read_text_handles_mixed_case_extensions(self) -> None:
        # Real filenames arrive in mixed case — Report.PDF / Deck.Docx —
        # and must still flow through parse_fn.  A case-sensitive check
        # used to bypass parsing and index raw-byte soup for these files.
        nx = MagicMock()
        nx.sys_read = MagicMock(return_value=b"%PDF-1.4 bytes")
        nx.metadata.get_file_metadata = MagicMock(return_value=None)
        nx.metadata.set_file_metadata = MagicMock()
        parse_fn = MagicMock(return_value=b"parsed markdown")
        reader = _NexusFSFileReader(nx, parse_fn=parse_fn)

        for path in ("/Report.PDF", "/Deck.Docx", "/Sheet.XLSX"):
            parse_fn.reset_mock()
            result = await reader.read_text(path)
            assert result == "parsed markdown", f"failed for {path}"
            parse_fn.assert_called_once()
