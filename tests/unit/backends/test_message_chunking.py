"""Tests for MessageBoundaryStrategy — per-message chunking for LLM conversations.

Tests cover:
- should_chunk: conversation detection (JSON array of message dicts)
- write_chunked + read_chunked: round-trip conversation through chunks
- Shared prefix dedup: two conversations sharing N messages → N shared chunks
- delete_chunked: cleanup
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _make_backend_and_strategy():
    """Create CASOpenAIBackend + MessageBoundaryStrategy."""
    from unittest.mock import MagicMock

    from nexus.backends.compute.message_chunking import MessageBoundaryStrategy
    from nexus.backends.compute.openai_compatible import CASOpenAIBackend

    with patch("nexus.backends.compute.openai_compatible._build_openai_client") as mock_build:
        mock_build.return_value = MagicMock()
        backend = CASOpenAIBackend(
            base_url="https://api.test.com/v1",
            api_key="sk-test",
        )

    strategy = MessageBoundaryStrategy(backend=backend)
    return backend, strategy


class TestShouldChunk:
    """Test conversation detection."""

    def test_valid_conversation(self) -> None:
        _, s = _make_backend_and_strategy()
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        assert s.should_chunk(json.dumps(msgs).encode()) is True

    def test_single_message_not_chunked(self) -> None:
        """Need at least 2 messages to chunk."""
        _, s = _make_backend_and_strategy()
        msgs = [{"role": "user", "content": "Hello"}]
        assert s.should_chunk(json.dumps(msgs).encode()) is False

    def test_non_json_not_chunked(self) -> None:
        _, s = _make_backend_and_strategy()
        assert s.should_chunk(b"not json") is False

    def test_non_array_not_chunked(self) -> None:
        _, s = _make_backend_and_strategy()
        assert s.should_chunk(json.dumps({"key": "value"}).encode()) is False

    def test_array_without_role_not_chunked(self) -> None:
        _, s = _make_backend_and_strategy()
        assert s.should_chunk(json.dumps([{"x": 1}, {"y": 2}]).encode()) is False


class TestWriteAndRead:
    """Test write_chunked + read_chunked round-trip."""

    def test_round_trip(self) -> None:
        backend, strategy = _make_backend_and_strategy()

        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
        ]
        content = json.dumps(msgs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        # Write
        manifest_hash = strategy.write_chunked(content)
        assert manifest_hash

        # Read
        result = strategy.read_chunked(manifest_hash)
        result_msgs = json.loads(result)
        assert len(result_msgs) == 3
        assert result_msgs[0]["role"] == "system"
        assert result_msgs[2]["content"] == "4"

    def test_is_chunked(self) -> None:
        backend, strategy = _make_backend_and_strategy()

        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        content = json.dumps(msgs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        manifest_hash = strategy.write_chunked(content)

        assert strategy.is_chunked(manifest_hash) is True
        assert strategy.is_chunked("nonexistent") is False

    def test_get_size(self) -> None:
        backend, strategy = _make_backend_and_strategy()

        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        content = json.dumps(msgs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        manifest_hash = strategy.write_chunked(content)

        assert strategy.get_size(manifest_hash) == len(content)


class TestSharedPrefixDedup:
    """Test that shared message prefixes are deduplicated in CAS."""

    def test_shared_prefix_chunks(self) -> None:
        """Two conversations sharing 2 messages → 2 shared chunks."""
        backend, strategy = _make_backend_and_strategy()

        shared = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]

        conv_a = shared + [{"role": "assistant", "content": "Hi! How can I help?"}]
        conv_b = shared + [{"role": "assistant", "content": "Hey there!"}]

        content_a = json.dumps(conv_a, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        content_b = json.dumps(conv_b, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        hash_a = strategy.write_chunked(content_a)
        hash_b = strategy.write_chunked(content_b)

        # Different manifests
        assert hash_a != hash_b

        # Read back manifests to check chunk sharing
        from nexus.backends.engines.cdc import ChunkedReference

        manifest_a_data, _ = backend._transport.fetch(backend._blob_key(hash_a))
        manifest_b_data, _ = backend._transport.fetch(backend._blob_key(hash_b))
        manifest_a = ChunkedReference.from_json(manifest_a_data)
        manifest_b = ChunkedReference.from_json(manifest_b_data)

        # First 2 chunk hashes are identical (shared prefix)
        assert manifest_a.chunks[0].chunk_hash == manifest_b.chunks[0].chunk_hash
        assert manifest_a.chunks[1].chunk_hash == manifest_b.chunks[1].chunk_hash

        # Third chunk hash differs
        assert manifest_a.chunks[2].chunk_hash != manifest_b.chunks[2].chunk_hash


class TestDeleteChunked:
    """Test chunked deletion (unconditional, no ref counting)."""

    def test_delete_removes_manifest_and_chunks(self) -> None:
        """Deleting a conversation removes its manifest and all chunks unconditionally."""
        backend, strategy = _make_backend_and_strategy()

        msgs = [
            {"role": "system", "content": "Shared."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "A"},
        ]
        content = json.dumps(msgs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        manifest_hash = strategy.write_chunked(content)
        assert strategy.is_chunked(manifest_hash)

        # Delete conversation
        strategy.delete_chunked(manifest_hash)

        # Manifest should be gone
        assert not strategy.is_chunked(manifest_hash)

    def test_delete_shared_chunks_breaks_other_conversation(self) -> None:
        """Without ref counting, deleting shared chunks makes the other conversation unreadable."""
        backend, strategy = _make_backend_and_strategy()

        shared = [
            {"role": "system", "content": "Shared."},
            {"role": "user", "content": "Hello"},
        ]
        conv_a = shared + [{"role": "assistant", "content": "A"}]
        conv_b = shared + [{"role": "assistant", "content": "B"}]

        content_a = json.dumps(conv_a, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        content_b = json.dumps(conv_b, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        hash_a = strategy.write_chunked(content_a)
        hash_b = strategy.write_chunked(content_b)

        # Delete conversation A — also deletes shared chunks
        strategy.delete_chunked(hash_a)

        # Manifest A gone
        assert not strategy.is_chunked(hash_a)

        # Conversation B manifest still exists but shared chunks are gone,
        # so reading it should fail (no ref counting to protect shared chunks).
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            strategy.read_chunked(hash_b)
