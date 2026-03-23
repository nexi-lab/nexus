"""Unit tests for RemoteContentFetcher protocol + CASRemoteContentFetcher (#1744 Phase 2).

Tests scatter-gather chunk fetch, local CAS check, manifest parsing,
and single-blob fast path — all CAS+CDC logic that was extracted from
FederationContentResolver into backends/base/.
"""

import json
from unittest.mock import MagicMock

import pytest

from nexus.backends.base.remote_content_fetcher import CASRemoteContentFetcher
from nexus.contracts.exceptions import NexusFileNotFoundError

ORIGIN_A = "10.0.0.1:50051"
ORIGIN_B = "10.0.0.2:50051"
ORIGIN_C = "10.0.0.3:50051"


def _make_manifest_bytes(chunk_hashes: list[str], total_size: int = 2048) -> bytes:
    """Build a minimal CDC manifest JSON bytes."""
    chunks = []
    offset = 0
    chunk_size = total_size // len(chunk_hashes) if chunk_hashes else 0
    for h in chunk_hashes:
        chunks.append({"chunk_hash": h, "offset": offset, "length": chunk_size})
        offset += chunk_size
    manifest = {
        "type": "chunked_manifest_v1",
        "total_size": total_size,
        "chunk_count": len(chunk_hashes),
        "avg_chunk_size": chunk_size,
        "content_hash": "full_content_hash_placeholder",
        "chunks": chunks,
    }
    return json.dumps(manifest, separators=(",", ":")).encode("utf-8")


def _make_fetcher(client=None, store=None):
    """Create a CASRemoteContentFetcher with mock dependencies."""
    return CASRemoteContentFetcher(
        peer_blob_client=client or MagicMock(),
        local_object_store=store or MagicMock(),
    )


class TestSingleBlobFile:
    """Non-chunked file: fetch by hash, store locally, return content."""

    def test_single_blob_stored_and_returned(self):
        mock_store = MagicMock()
        mock_store.content_exists.return_value = False

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = b"hello world"

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A], "single_hash")

        assert result == b"hello world"
        mock_client.fetch_blob.assert_called_once_with(ORIGIN_A, "single_hash")
        mock_store.write_content.assert_called_once_with(b"hello world")

    def test_content_already_local_skips_remote(self):
        """Content in local CAS: zero remote fetch."""
        mock_store = MagicMock()
        mock_store.content_exists.return_value = True
        mock_store.read_content.return_value = b"local content"

        mock_client = MagicMock()

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A], "local_hash")

        assert result == b"local content"
        mock_client.fetch_blob.assert_not_called()


class TestChunkedFileSingleOrigin:
    """Chunked file with a single origin — no scatter-gather needed."""

    def test_all_chunks_missing(self):
        """All chunks fetched from single origin."""
        chunk_hashes = ["chunk_a", "chunk_b", "chunk_c"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs.return_value = {
            "chunk_a": b"aaa",
            "chunk_b": b"bbb",
            "chunk_c": b"ccc",
        }

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A], "manifest_hash")

        assert result == b"assembled"
        mock_client.fetch_blob.assert_called_once_with(ORIGIN_A, "manifest_hash")
        # Single origin → uses fetch_blobs (not scatter)
        mock_client.fetch_blobs.assert_called_once()
        fetched_hashes = mock_client.fetch_blobs.call_args[0][1]
        assert set(fetched_hashes) == {"chunk_a", "chunk_b", "chunk_c"}
        mock_store.read_content.assert_called_once_with("manifest_hash")

    def test_partial_local_chunks(self):
        """Only missing chunks are fetched."""
        chunk_hashes = ["chunk_a", "chunk_b", "chunk_c"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        mock_store.content_exists.side_effect = lambda h: h in ("chunk_a", "chunk_c")
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs.return_value = {"chunk_b": b"bbb"}

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A], "manifest_hash")

        assert result == b"assembled"
        fetched_hashes = mock_client.fetch_blobs.call_args[0][1]
        assert fetched_hashes == ["chunk_b"]

    def test_all_chunks_local(self):
        """All chunks in local CAS: manifest fetched but no chunk fetch."""
        chunk_hashes = ["chunk_a", "chunk_b"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        # content_exists: manifest_hash=no (triggers remote fetch),
        # but chunks are all local
        mock_store.content_exists.side_effect = lambda h: h != "manifest_hash"
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A], "manifest_hash")

        assert result == b"assembled"
        # Manifest fetched, but no chunk fetch needed
        mock_client.fetch_blob.assert_called_once()
        mock_client.fetch_blobs.assert_not_called()


class TestScatterGatherMultiOrigin:
    """Scatter-gather: chunks fetched from multiple origins in parallel."""

    def test_scatter_across_two_origins(self):
        """Multi-origin → uses fetch_blobs_scatter instead of fetch_blobs."""
        chunk_hashes = ["chunk_a", "chunk_b", "chunk_c", "chunk_d"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs_scatter.return_value = {
            "chunk_a": b"aaa",
            "chunk_b": b"bbb",
            "chunk_c": b"ccc",
            "chunk_d": b"ddd",
        }

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A, ORIGIN_B], "manifest_hash")

        assert result == b"assembled"
        # Uses scatter variant (not single-origin fetch_blobs)
        mock_client.fetch_blobs_scatter.assert_called_once()
        call_args = mock_client.fetch_blobs_scatter.call_args
        assert call_args[0][0] == [ORIGIN_A, ORIGIN_B]
        assert set(call_args[0][1]) == {"chunk_a", "chunk_b", "chunk_c", "chunk_d"}
        # NOT called
        mock_client.fetch_blobs.assert_not_called()

    def test_scatter_with_partial_local(self):
        """Scatter-gather only fetches missing chunks."""
        chunk_hashes = ["chunk_a", "chunk_b", "chunk_c"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        mock_store.content_exists.side_effect = lambda h: h == "chunk_a"
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs_scatter.return_value = {
            "chunk_b": b"bbb",
            "chunk_c": b"ccc",
        }

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A, ORIGIN_B], "manifest_hash")

        assert result == b"assembled"
        call_args = mock_client.fetch_blobs_scatter.call_args
        assert set(call_args[0][1]) == {"chunk_b", "chunk_c"}

    def test_scatter_three_origins(self):
        """Works with 3+ origins."""
        chunk_hashes = ["chunk_x"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs_scatter.return_value = {"chunk_x": b"xxx"}

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content(
            [ORIGIN_A, ORIGIN_B, ORIGIN_C],
            "manifest_hash",
        )

        assert result == b"assembled"
        call_args = mock_client.fetch_blobs_scatter.call_args
        assert call_args[0][0] == [ORIGIN_A, ORIGIN_B, ORIGIN_C]


class TestManifestFetchFailover:
    """Manifest blob itself may need to fail over across origins."""

    def test_manifest_failover_to_second_origin(self):
        """First origin 404 on manifest, second succeeds."""
        mock_store = MagicMock()
        mock_store.content_exists.return_value = False

        mock_client = MagicMock()
        mock_client.fetch_blob.side_effect = [
            NexusFileNotFoundError("manifest_hash", "not on A"),
            b"simple content",  # Not a manifest
        ]

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        result = fetcher.fetch_remote_content([ORIGIN_A, ORIGIN_B], "manifest_hash")

        assert result == b"simple content"
        assert mock_client.fetch_blob.call_count == 2

    def test_all_origins_fail_raises(self):
        """All origins 404 on manifest → raises NexusFileNotFoundError."""
        mock_store = MagicMock()
        mock_store.content_exists.return_value = False

        mock_client = MagicMock()
        mock_client.fetch_blob.side_effect = NexusFileNotFoundError(
            "manifest_hash",
            "not found",
        )

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        with pytest.raises(NexusFileNotFoundError, match="not found on any origin"):
            fetcher.fetch_remote_content([ORIGIN_A, ORIGIN_B], "manifest_hash")


class TestChunkStorage:
    """Verify chunks and manifest are stored locally after fetch."""

    def test_chunks_and_manifest_stored(self):
        """Both fetched chunks and manifest are written to local store."""
        chunk_hashes = ["chunk_a", "chunk_b"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs.return_value = {
            "chunk_a": b"aaa",
            "chunk_b": b"bbb",
        }

        fetcher = _make_fetcher(client=mock_client, store=mock_store)
        fetcher.fetch_remote_content([ORIGIN_A], "manifest_hash")

        # write_content called for: chunk_a, chunk_b, manifest
        assert mock_store.write_content.call_count == 3
        written = [call.args[0] for call in mock_store.write_content.call_args_list]
        assert b"aaa" in written
        assert b"bbb" in written
        assert manifest_bytes in written
