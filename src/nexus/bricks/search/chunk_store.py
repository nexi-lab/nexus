"""Shared document chunk persistence for search indexing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text


@dataclass(frozen=True)
class ChunkRecord:
    """Normalized document chunk row payload."""

    chunk_text: str
    chunk_tokens: int
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    chunk_context: str | None = None
    chunk_position: int | None = None
    source_document_id: str | None = None


class ChunkStore:
    """Canonical writer for document_chunks replacement semantics."""

    def __init__(self, *, async_session_factory: Any, db_type: str = "sqlite") -> None:
        self._async_session_factory = async_session_factory
        self._db_type = db_type

    async def delete_document_chunks(self, path_id: str) -> None:
        async with self._async_session_factory() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE path_id = :path_id"),
                {"path_id": path_id},
            )
            await session.commit()

    async def replace_document_chunks(self, path_id: str, chunks: list[ChunkRecord]) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._async_session_factory() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE path_id = :path_id"),
                {"path_id": path_id},
            )

            plain_rows: list[dict[str, Any]] = []
            embedded_rows: list[dict[str, Any]] = []
            for index, chunk in enumerate(chunks):
                params = self._build_insert_params(path_id, index, chunk, now)
                if chunk.embedding is not None and self._db_type == "postgresql":
                    embedded_rows.append(params)
                else:
                    plain_rows.append(params)

            if plain_rows:
                await session.execute(self._insert_sql(include_embedding=False), plain_rows)
            if embedded_rows:
                await session.execute(self._insert_sql(include_embedding=True), embedded_rows)

            await session.commit()

    def _build_insert_params(
        self,
        path_id: str,
        index: int,
        chunk: ChunkRecord,
        now: datetime,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chunk_id": str(uuid.uuid4()),
            "path_id": path_id,
            "chunk_index": index,
            "chunk_text": chunk.chunk_text,
            "chunk_tokens": chunk.chunk_tokens,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
            "line_start": chunk.line_start,
            "line_end": chunk.line_end,
            "embedding_model": chunk.embedding_model,
            "chunk_context": chunk.chunk_context,
            "chunk_position": chunk.chunk_position,
            "source_document_id": chunk.source_document_id,
            "created_at": now,
        }
        if chunk.embedding is not None and self._db_type == "postgresql":
            params["embedding"] = "[" + ",".join(str(v) for v in chunk.embedding) + "]"
        return params

    def _insert_sql(self, *, include_embedding: bool) -> Any:
        if include_embedding and self._db_type == "postgresql":
            return text(
                """
                INSERT INTO document_chunks
                (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens,
                 start_offset, end_offset, line_start, line_end,
                 embedding_model, embedding,
                 chunk_context, chunk_position, source_document_id,
                 created_at)
                VALUES
                (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens,
                 :start_offset, :end_offset, :line_start, :line_end,
                 :embedding_model, CAST(:embedding AS halfvec),
                 :chunk_context, :chunk_position, :source_document_id,
                 :created_at)
                """
            )
        return text(
            """
            INSERT INTO document_chunks
            (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens,
             start_offset, end_offset, line_start, line_end,
             embedding_model,
             chunk_context, chunk_position, source_document_id,
             created_at)
            VALUES
            (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens,
             :start_offset, :end_offset, :line_start, :line_end,
             :embedding_model,
             :chunk_context, :chunk_position, :source_document_id,
             :created_at)
            """
        )
