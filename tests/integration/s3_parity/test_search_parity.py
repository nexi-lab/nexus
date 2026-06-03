"""S3-vs-local semantic-search parity (#4267).

Proves the substantive search criterion: a file stored on S3 (Rust driver-s3 →
MinIO) is read by the PRODUCTION indexing file-reader identically to a
local-backed file, and the indexed content is **semantically** retrievable
identically to local.

Scope note (op-log → daemon): the literal "write → operation_log → search
daemon indexes it" path is backend-AGNOSTIC machinery — the Rust kernel logs
every write to its operation_log regardless of backend, and the async search
daemon consumes that log the same way for any backend. That end-to-end daemon
path runs only in the full server stack and is tracked by the full-`nexus up`
follow-up (#4308). What is backend-SPECIFIC — and therefore what this suite
proves — is the two steps below:

  1. the indexer's file-read step returns identical bytes from the S3 mount and
     the local mount (the only backend-dependent step in indexing), and
  2. that content, once indexed, is semantically retrievable identically.

Requires the SANDBOX search extra (``fastembed`` + ``sqlite-vec``); skips
cleanly otherwise. The first run downloads a small ONNX embedding model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastembed", reason="fastembed required for in-process semantic search")
pytest.importorskip("sqlite_vec", reason="sqlite-vec required for in-process semantic search")

from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.factory.adapters import _NexusFSFileReader

# Distinctive content with NO lexical overlap with the query, so a hit proves
# genuine semantic (vector) retrieval rather than a keyword match.
_DOC = (
    "The mitochondrion is the powerhouse of the cell; it generates ATP through "
    "oxidative phosphorylation in eukaryotic organisms."
)
_QUERY = "what organelle produces cellular energy"


def _backend(tmp_path: Path) -> SqliteVecBackend:
    # Explicit fastembed embedder → offline ONNX, no API key / network beyond
    # the one-time model fetch. 384-dim BAAI/bge-small-en-v1.5.
    return SqliteVecBackend(db_path=str(tmp_path / "vec.db"), embedder="fastembed")


# Substrings that indicate the offline embedding MODEL itself is unavailable
# (no network for the one-time HF fetch, ONNX runtime issue, etc.) — distinct
# from a genuine parity failure. We skip on these, never on assertion errors.
_MODEL_UNAVAILABLE_MARKERS = (
    "model",
    "download",
    "huggingface",
    "hf_hub",
    "onnx",
    "connection",
    "network",
    "fetch",
    "resolve",
    "timeout",
    "offline",
)


async def _embed_or_skip(backend: SqliteVecBackend, docs: list[dict]) -> None:
    """Upsert docs; skip (don't fail) if the offline model can't be loaded."""
    try:
        await backend.upsert(docs, zone_id=ROOT_ZONE_ID)
    except AssertionError:
        raise  # a real parity/shape failure — never mask it
    except Exception as exc:  # noqa: BLE001
        blob = f"{type(exc).__name__}: {exc}".lower()
        if any(m in blob for m in _MODEL_UNAVAILABLE_MARKERS):
            pytest.skip(f"offline embedding model unavailable: {exc}")
        raise


@pytest.mark.integration
class TestSearchParity:
    async def test_production_reader_reads_s3_content_like_local(self, parity_kernel):
        """The indexing file-reader (_NexusFSFileReader) returns identical text
        for an S3-backed file and a local-backed file."""
        h = parity_kernel
        local_p, s3_p = h.paths("bio.txt")
        h.fs.write(local_p, _DOC.encode(), context=h.ctx)
        h.fs.write(s3_p, _DOC.encode(), context=h.ctx)

        reader = _NexusFSFileReader(h.fs, parse_fn=None)
        local_text = await reader.read_text(local_p)
        s3_text = await reader.read_text(s3_p)
        assert s3_text == local_text == _DOC

    async def test_semantic_search_parity(self, parity_kernel, tmp_path):
        """A file on S3 is semantically retrievable identically to local."""
        h = parity_kernel
        local_p, s3_p = h.paths("bio.txt")
        h.fs.write(local_p, _DOC.encode(), context=h.ctx)
        h.fs.write(s3_p, _DOC.encode(), context=h.ctx)

        reader = _NexusFSFileReader(h.fs, parse_fn=None)
        backend = _backend(tmp_path)
        try:
            # Index the content as read FROM each mount (S3 content came from MinIO).
            await _embed_or_skip(
                backend,
                [
                    {"path": s3_p, "text": await reader.read_text(s3_p)},
                    {"path": local_p, "text": await reader.read_text(local_p)},
                ],
            )
            hits = await backend.search(_QUERY, limit=5, zone_id=ROOT_ZONE_ID)
        finally:
            await backend.shutdown()

        by_path = {getattr(h_, "path", None): h_ for h_ in hits}
        # Both backends' files are retrieved by the same semantic query...
        assert s3_p in by_path, f"S3 file not retrieved; got {list(by_path)}"
        assert local_p in by_path, f"local file not retrieved; got {list(by_path)}"
        # ...and the indexed S3 content matches the local content (parity).
        assert by_path[s3_p].chunk_text == by_path[local_p].chunk_text == _DOC
