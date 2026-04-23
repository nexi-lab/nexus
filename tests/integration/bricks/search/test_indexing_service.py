"""TDD tests for IndexingService (Issue #1094).

Tests the unified indexing service that wraps IndexingPipeline + FileReaderProtocol.
Validates content-hash skip logic, delegation to pipeline, directory indexing,
delete operations, stats retrieval, and atomic reindex safety (Issue #2753).
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.indexing import IndexResult
from nexus.bricks.search.indexing_service import IndexingService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_file_model(
    path_id: str = "pid-1",
    content_hash: str | None = "abc123",
    indexed_content_hash: str | None = None,
    virtual_path: str = "test.py",
) -> MagicMock:
    """Create a mock FilePathModel with configurable hash fields."""
    model = MagicMock()
    model.path_id = path_id
    model.content_hash = content_hash
    model.indexed_content_hash = indexed_content_hash
    model.virtual_path = virtual_path
    model.last_indexed_at = None
    return model


def _mock_session(
    file_model: MagicMock | None = None,
    chunk_count: int = 0,
) -> MagicMock:
    """Create a mock SQLAlchemy session.

    Configures execute() to return the file_model for select queries and
    supports session.get() for post-indexing model retrieval.
    """
    session = MagicMock()

    # execute().scalar_one_or_none() returns the file_model (for _query_file_model)
    # execute().scalar() returns chunk_count (for _count_chunks and get_index_stats)
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = file_model
    scalar_result.scalar.return_value = chunk_count
    session.execute.return_value = scalar_result

    # session.get() used in step 5 to retrieve updated file_model
    session.get.return_value = file_model
    session.commit = MagicMock()

    return session


def _mock_session_factory(session: MagicMock) -> MagicMock:
    """Return a session_factory mock (unused directly by IndexingService)."""
    return MagicMock()


def _mock_file_reader(
    session: MagicMock,
    content: str = "file content",
    searchable_text: str | None = None,
    file_list: list[str] | None = None,
) -> MagicMock:
    """Create a mock FileReaderProtocol.

    get_session() returns a context manager yielding the given session.
    read_text() returns content, get_searchable_text() returns searchable_text.
    list_files() returns file_list.
    """
    reader = MagicMock()

    @contextmanager
    def _get_session():
        yield session

    reader.get_session = _get_session
    reader.read_text = AsyncMock(return_value=content)
    reader.get_searchable_text.return_value = searchable_text
    reader.list_files = AsyncMock(return_value=file_list or [])

    return reader


def _mock_pipeline() -> MagicMock:
    """Create a mock IndexingPipeline with async methods."""
    pipeline = MagicMock()
    pipeline.index_document = AsyncMock()
    pipeline.index_documents = AsyncMock()
    return pipeline


def _mock_vector_db() -> MagicMock:
    """Create a mock VectorDatabase."""
    vdb = MagicMock()
    vdb.get_stats.return_value = {"backend": "mock"}
    return vdb


def _build_service(
    *,
    pipeline: MagicMock | None = None,
    file_reader: MagicMock | None = None,
    session: MagicMock | None = None,
    vector_db: MagicMock | None = None,
    embedding_provider: MagicMock | None = None,
    file_model: MagicMock | None = None,
    content: str = "file content",
    searchable_text: str | None = None,
    file_list: list[str] | None = None,
    chunk_count: int = 0,
) -> tuple[IndexingService, MagicMock, MagicMock, MagicMock]:
    """Convenience builder that wires together all mocks.

    Returns (service, pipeline, session, file_reader).
    """
    if file_model is None:
        file_model = _mock_file_model()
    if session is None:
        session = _mock_session(file_model=file_model, chunk_count=chunk_count)
    if file_reader is None:
        file_reader = _mock_file_reader(
            session,
            content=content,
            searchable_text=searchable_text,
            file_list=file_list,
        )
    if pipeline is None:
        pipeline = _mock_pipeline()
    if vector_db is None:
        vector_db = _mock_vector_db()

    service = IndexingService(
        pipeline=pipeline,
        file_reader=file_reader,
        session_factory=MagicMock(),
        vector_db=vector_db,
        embedding_provider=embedding_provider,
    )
    return service, pipeline, session, file_reader


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIndexDocument:
    @pytest.mark.asyncio
    async def test_index_document_delegates_to_pipeline(self) -> None:
        """index_document reads content and delegates to pipeline.index_document."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="abc123",
            indexed_content_hash=None,
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.return_value = IndexResult(
            path="test.py",
            chunks_indexed=5,
        )

        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="hello world",
        )

        result = await service.index_document("test.py")

        assert result == 5
        pipeline.index_document.assert_awaited_once_with(
            "test.py",
            "hello world",
            "pid-1",
        )

    @pytest.mark.asyncio
    async def test_index_document_skips_unchanged_content_hash(self) -> None:
        """When content_hash == indexed_content_hash, pipeline is NOT called."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="same_hash",
            indexed_content_hash="same_hash",
        )

        service, pipeline, session, _ = _build_service(
            file_model=file_model,
            chunk_count=7,
        )

        result = await service.index_document("test.py")

        assert result == 7
        pipeline.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_index_document_updates_hash_after_indexing(self) -> None:
        """After successful indexing, indexed_content_hash and last_indexed_at are set."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="new_hash",
            indexed_content_hash=None,
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.return_value = IndexResult(
            path="test.py",
            chunks_indexed=3,
        )

        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
        )

        await service.index_document("test.py")

        # session.get() is called in step 5 to retrieve the model, then fields are set
        assert file_model.indexed_content_hash == "new_hash"
        assert file_model.last_indexed_at is not None
        assert isinstance(file_model.last_indexed_at, datetime)

    @pytest.mark.asyncio
    async def test_index_document_force_bypasses_hash_check(self) -> None:
        """force=True bypasses content-hash skip logic, pipeline IS called."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="same_hash",
            indexed_content_hash="same_hash",
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.return_value = IndexResult(
            path="test.py",
            chunks_indexed=4,
        )

        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="forced content",
        )

        result = await service.index_document("test.py", force=True)

        assert result == 4
        pipeline.index_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_index_document_empty_returns_zero(self) -> None:
        """Pipeline returns IndexResult(chunks_indexed=0) -> service returns 0."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="abc",
            indexed_content_hash=None,
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.return_value = IndexResult(
            path="empty.py",
            chunks_indexed=0,
        )

        service, _, _, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="",
        )

        result = await service.index_document("empty.py")

        assert result == 0

    @pytest.mark.asyncio
    async def test_index_document_does_not_latch_failed_parse_for_pdf(self) -> None:
        """Parseable binary with empty content (parser down) must NOT advance
        indexed_content_hash — otherwise the hash-match fast path on the
        next run skips the file forever and we get a silent search hole.
        """
        file_model = _mock_file_model(
            path_id="pid-pdf",
            content_hash="abc-pdf",
            indexed_content_hash=None,
            virtual_path="/doc.pdf",
        )
        pipeline = _mock_pipeline()

        # Empty content simulates read_text() failing closed for a PDF
        # when parse_fn is missing or raised.
        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="",
        )

        result = await service.index_document("/doc.pdf")

        assert result == 0
        # Critical: pipeline must NOT be invoked and hash must NOT advance.
        pipeline.index_document.assert_not_called()
        assert file_model.indexed_content_hash is None
        assert file_model.last_indexed_at is None
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_document_clears_stale_chunks_when_parse_fails_on_changed_pdf(
        self,
    ) -> None:
        """If a PDF previously had chunks indexed and its content has since
        changed, but the parser now fails, leaving the old chunks live would
        keep outdated text searchable.  Verify we delete them instead.
        """
        file_model = _mock_file_model(
            path_id="pid-pdf-changed",
            content_hash="new-hash",
            indexed_content_hash="old-hash",  # was previously indexed with different content
            virtual_path="/changed.pdf",
        )
        pipeline = _mock_pipeline()
        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="",
        )

        result = await service.index_document("/changed.pdf")

        assert result == 0
        # Pipeline must not run (nothing to index).
        pipeline.index_document.assert_not_called()
        # But a DELETE against document_chunks must have been issued.
        delete_calls = [
            call
            for call in session.execute.call_args_list
            if "delete" in str(call).lower() or "DELETE" in str(call)
        ]
        assert delete_calls, "expected a DELETE on stale document_chunks"
        # Tracking fields must still remain untouched so the next tick retries.
        assert file_model.indexed_content_hash == "old-hash"
        assert file_model.last_indexed_at is None

    @pytest.mark.asyncio
    async def test_index_document_advances_hash_on_successful_empty_parse(self) -> None:
        """Image-only / blank PDFs legitimately produce zero searchable text.
        The indexer must advance ``indexed_content_hash`` with zero chunks in
        that case — otherwise the file stays perpetually 'unindexed' and we
        burn CPU reparsing it on every tick.  The adapter probes for this
        via ``has_successful_parse`` (matching ``parsed_text_hash``).

        It must also DELETE any existing chunks for the path_id, otherwise
        a non-empty previous revision would keep serving stale text forever
        (the normal hash-match skip path means this row is never revisited).
        """
        file_model = _mock_file_model(
            path_id="pid-pdf-empty",
            content_hash="blake3-hash-of-blank-pdf",
            indexed_content_hash=None,
            virtual_path="/blank.pdf",
        )
        pipeline = _mock_pipeline()
        service, _, session, file_reader = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="",  # empty — parser succeeded but no text extracted
        )
        # Adapter reports: yes, this empty is a valid successful parse.
        file_reader.has_successful_parse = MagicMock(return_value=True)

        result = await service.index_document("/blank.pdf")

        assert result == 0
        # No pipeline work (nothing to chunk / embed).
        pipeline.index_document.assert_not_called()
        # But indexed_content_hash MUST advance so we don't retry forever.
        assert file_model.indexed_content_hash == "blake3-hash-of-blank-pdf"
        session.commit.assert_called_once()
        # DELETE must have been issued so a prior non-empty revision's
        # chunks don't keep serving stale text.
        delete_calls = [
            call
            for call in session.execute.call_args_list
            if "delete" in str(call).lower() or "DELETE" in str(call)
        ]
        assert delete_calls, "expected DELETE on document_chunks during empty-parse replace"
        file_reader.has_successful_parse.assert_called_once_with(
            "/blank.pdf", "blake3-hash-of-blank-pdf"
        )

    @pytest.mark.asyncio
    async def test_index_document_retries_on_parse_error_when_hash_unmatched(self) -> None:
        """Distinct from the successful-empty case: when ``has_successful_parse``
        returns False (parser broken, file never parsed), the retry path
        must still fire — don't latch the failure.
        """
        file_model = _mock_file_model(
            path_id="pid-pdf-broken",
            content_hash="blake3-hash-of-broken-pdf",
            indexed_content_hash=None,
            virtual_path="/broken.pdf",
        )
        pipeline = _mock_pipeline()
        service, _, session, file_reader = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="",
        )
        # No record of a successful parse.
        file_reader.has_successful_parse = MagicMock(return_value=False)

        result = await service.index_document("/broken.pdf")

        assert result == 0
        pipeline.index_document.assert_not_called()
        # Tracking fields stay untouched so the next tick retries.
        assert file_model.indexed_content_hash is None
        assert file_model.last_indexed_at is None
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_document_skips_stale_delete_when_concurrent_reindex_advanced_hash(
        self,
    ) -> None:
        """A successful concurrent reindex can advance ``indexed_content_hash``
        between the first session snapshot and the stale-chunk delete session.
        The CAS guard must NOT delete when the re-read hash differs from the
        stale value we observed — otherwise we'd wipe freshly-indexed chunks.
        """
        # Step-1 snapshot shows (content=new-hash, indexed=old-hash), so
        # the delete path is entered.  But between step 1 and the delete
        # session, a concurrent run completes: the row now reads
        # (content=new-hash, indexed=new-hash).
        step1_model = _mock_file_model(
            path_id="pid-pdf-cas",
            content_hash="new-hash",
            indexed_content_hash="old-hash",
            virtual_path="/cas.pdf",
        )
        step2_model = _mock_file_model(
            path_id="pid-pdf-cas",
            content_hash="new-hash",
            indexed_content_hash="new-hash",  # fresh index landed
            virtual_path="/cas.pdf",
        )

        # First call returns the stale snapshot, second call returns the
        # post-reindex row.  scalar_one_or_none drives both the step-1
        # lookup and the CAS re-read.
        scalar_result_stale = MagicMock()
        scalar_result_stale.scalar_one_or_none.return_value = step1_model
        scalar_result_stale.scalar.return_value = 0
        scalar_result_fresh = MagicMock()
        scalar_result_fresh.scalar_one_or_none.return_value = step2_model
        scalar_result_fresh.scalar.return_value = 0

        session = MagicMock()
        session.execute.side_effect = [scalar_result_stale, scalar_result_fresh]
        session.get.return_value = step2_model
        session.commit = MagicMock()

        pipeline = _mock_pipeline()
        file_reader = _mock_file_reader(session, content="", searchable_text=None)
        service = IndexingService(
            pipeline=pipeline,
            file_reader=file_reader,
            session_factory=MagicMock(),
            vector_db=_mock_vector_db(),
            embedding_provider=None,
        )

        result = await service.index_document("/cas.pdf")

        assert result == 0
        pipeline.index_document.assert_not_called()
        # Critical: no commit (therefore no DELETE) must fire in the CAS-miss
        # branch.  If the CAS guard failed we'd see exactly one commit for
        # the stale-chunk delete.
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_document_handles_mixed_case_pdf_extension(self) -> None:
        """Mixed-case extensions (``/Report.PDF``) must route through the
        parseable branch so fail-closed behavior covers them.  Without the
        case-insensitive helper, they slip through and get latched as
        empty-content text files.
        """
        file_model = _mock_file_model(
            path_id="pid-pdf-upper",
            content_hash="abc",
            indexed_content_hash=None,
            virtual_path="/Report.PDF",
        )
        pipeline = _mock_pipeline()
        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="",  # parse failed
        )

        result = await service.index_document("/Report.PDF")

        assert result == 0
        pipeline.index_document.assert_not_called()
        assert file_model.indexed_content_hash is None
        session.commit.assert_not_called()


class TestIndexDirectory:
    @pytest.mark.asyncio
    async def test_index_directory_builds_doc_list_and_delegates(self) -> None:
        """list_files returns 3 files; pipeline.index_documents receives 3 tuples."""
        file_model = _mock_file_model(path_id="pid-x")
        pipeline = _mock_pipeline()
        pipeline.index_documents.return_value = [
            IndexResult(path="a.py", chunks_indexed=2),
            IndexResult(path="b.py", chunks_indexed=3),
            IndexResult(path="c.py", chunks_indexed=1),
        ]

        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            file_list=["a.py", "b.py", "c.py"],
            content="some content",
            searchable_text=None,
        )

        results = await service.index_directory("/")

        assert len(results) == 3
        assert "a.py" in results
        assert "b.py" in results
        assert "c.py" in results

        # Verify pipeline received 3 document tuples
        pipeline.index_documents.assert_awaited_once()
        docs_arg = pipeline.index_documents.call_args[0][0]
        assert len(docs_arg) == 3
        # Each tuple is (path, content, path_id)
        for _path, content, path_id in docs_arg:
            assert content == "some content"
            assert path_id == "pid-x"


class TestVirtualReadmeIndexing:
    """Issue #3728: virtual ``.readme/`` overlay paths have no
    FilePathModel rows but should still be indexed for semantic search."""

    def test_looks_like_virtual_readme_helper(self) -> None:
        from nexus.bricks.search.indexing_service import _looks_like_virtual_readme

        # Exact ``.readme`` segment matches
        assert _looks_like_virtual_readme("/gws/gmail/.readme/README.md") is True
        assert _looks_like_virtual_readme("/gws/gmail/.readme/schemas/send.yaml") is True
        assert _looks_like_virtual_readme("/.readme/README.md") is True

        # Not matches
        assert _looks_like_virtual_readme("/a/b/c.txt") is False
        assert _looks_like_virtual_readme("/gws/gmail/INBOX/msg.yaml") is False

        # Partial-name false positives rejected — segment must be exactly ``.readme``
        assert _looks_like_virtual_readme("/work/my.readme/file.md") is False
        assert _looks_like_virtual_readme("/work/.readmex/file.md") is False

    def test_virtual_path_id_is_deterministic_and_prefixed(self) -> None:
        from nexus.bricks.search.indexing_service import _virtual_path_id

        a = _virtual_path_id("/gws/gmail/.readme/README.md")
        b = _virtual_path_id("/gws/gmail/.readme/README.md")
        c = _virtual_path_id("/gws/gmail/.readme/schemas/send.yaml")
        assert a == b  # deterministic
        assert a != c  # different paths → different ids
        assert a.startswith("virtual:")
        # Uses SHA-256 (64 hex chars after the prefix)
        assert len(a) == len("virtual:") + 64

    @pytest.mark.asyncio
    async def test_index_directory_indexes_virtual_readme_paths_without_row(self) -> None:
        """Virtual ``.readme/`` files have no FilePathModel but should still
        be indexed via a synthetic ``virtual:`` path_id."""
        # Session returns None for every FilePathModel query → simulates
        # a post-mount indexing run over a skill backend where only the
        # virtual tree has entries and the metastore has no rows for
        # ``.readme/*``.
        session = _mock_session(file_model=None)

        pipeline = _mock_pipeline()
        pipeline.index_documents.return_value = [
            IndexResult(path="/gws/gmail/.readme/README.md", chunks_indexed=4),
            IndexResult(path="/gws/gmail/.readme/schemas/send.yaml", chunks_indexed=2),
        ]

        file_list = [
            "/gws/gmail/.readme/README.md",
            "/gws/gmail/.readme/schemas/send.yaml",
        ]
        service, _, _, _ = _build_service(
            pipeline=pipeline,
            session=session,
            file_list=file_list,
            content="# Gmail\n\nsend_email schema ...",
        )

        results = await service.index_directory("/gws/gmail/")

        # Both virtual paths were indexed even though _query_file_model
        # returned None for each.
        assert len(results) == 2
        pipeline.index_documents.assert_awaited_once()
        docs = pipeline.index_documents.call_args[0][0]
        assert len(docs) == 2
        for doc_path, content, path_id in docs:
            assert doc_path in file_list
            assert content == "# Gmail\n\nsend_email schema ..."
            # Synthetic path_id is used instead of a real FilePathModel id
            assert path_id.startswith("virtual:")

    @pytest.mark.asyncio
    async def test_index_directory_still_skips_rowless_non_readme_paths(self) -> None:
        """A real (non-``.readme/``) file with no FilePathModel row is
        still skipped — the virtual fallback must not catch random
        row-less paths."""
        session = _mock_session(file_model=None)  # all queries miss

        pipeline = _mock_pipeline()
        pipeline.index_documents.return_value = []

        service, _, _, _ = _build_service(
            pipeline=pipeline,
            session=session,
            file_list=["/work/notes.txt", "/work/code.py"],
            content="some content",
        )

        await service.index_directory("/work/")

        # No documents appended — both rows are missing AND they're not
        # under ``.readme/``, so the indexer drops them like before.
        assert pipeline.index_documents.await_count in (0, 1)
        if pipeline.index_documents.await_count == 1:
            docs = pipeline.index_documents.call_args[0][0]
            assert docs == []


class TestDeleteDocumentIndex:
    @pytest.mark.asyncio
    async def test_delete_document_index_removes_chunks(self) -> None:
        """delete_document_index executes DELETE and commits."""
        file_model = _mock_file_model(path_id="pid-del")

        service, _, session, _ = _build_service(file_model=file_model)

        await service.delete_document_index("test.py")

        # Verify session.execute was called with a delete statement
        session.execute.assert_called()
        session.commit.assert_called()


class TestAtomicReindex:
    """Tests for atomic reindex safety (Issue #2753).

    Verifies that pipeline failure does NOT leave the index in an incomplete
    state — old chunks must remain until new chunks are fully committed.
    """

    async def test_pipeline_failure_does_not_delete_old_chunks(self) -> None:
        """When pipeline.index_document raises, no DELETE is executed on the session."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="new_hash",
            indexed_content_hash="old_hash",
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.side_effect = ConnectionError("embedding API timeout")

        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="updated content",
        )

        async def run():
            with pytest.raises(ConnectionError, match="embedding API timeout"):
                await service.index_document("test.py")

        asyncio.run(run())

        # The service should NOT have called session.execute with a DELETE
        # (the old premature delete was removed in Issue #2753).
        for call in session.execute.call_args_list:
            stmt = call[0][0]
            stmt_str = str(stmt)
            assert "DELETE" not in stmt_str.upper() or "document_chunks" not in stmt_str

    async def test_successful_reindex_delegates_delete_to_pipeline(self) -> None:
        """On success, pipeline handles delete+insert atomically (no service-level delete)."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="new_hash",
            indexed_content_hash="old_hash",
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.return_value = IndexResult(path="test.py", chunks_indexed=10)

        service, _, session, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
            content="new content",
        )

        async def run():
            return await service.index_document("test.py")

        result = asyncio.run(run())

        assert result == 10
        pipeline.index_document.assert_awaited_once_with("test.py", "new content", "pid-1")
        # No DELETE from the service layer — pipeline owns the atomic swap
        for call in session.execute.call_args_list:
            stmt = call[0][0]
            stmt_str = str(stmt)
            assert "DELETE" not in stmt_str.upper() or "document_chunks" not in stmt_str

    async def test_hash_not_updated_on_pipeline_failure(self) -> None:
        """When pipeline fails, indexed_content_hash must NOT be updated."""
        file_model = _mock_file_model(
            path_id="pid-1",
            content_hash="new_hash",
            indexed_content_hash="old_hash",
        )
        pipeline = _mock_pipeline()
        pipeline.index_document.side_effect = RuntimeError("chunking error")

        service, _, _, _ = _build_service(
            pipeline=pipeline,
            file_model=file_model,
        )

        async def run():
            with pytest.raises(RuntimeError, match="chunking error"):
                await service.index_document("test.py")

        asyncio.run(run())

        # indexed_content_hash should remain unchanged
        assert file_model.indexed_content_hash == "old_hash"


class TestGetIndexStats:
    @pytest.mark.asyncio
    async def test_get_index_stats_returns_shape(self) -> None:
        """get_index_stats returns dict with expected keys."""
        file_model = _mock_file_model()
        session = _mock_session(file_model=file_model, chunk_count=42)
        vector_db = _mock_vector_db()

        service, _, _, _ = _build_service(
            session=session,
            file_model=file_model,
            vector_db=vector_db,
        )

        stats = await service.get_index_stats()

        assert "total_chunks" in stats
        assert "indexed_files" in stats
        assert "embedding_provider" in stats
        assert "vector_db" in stats
        assert stats["vector_db"] == {"backend": "mock"}
