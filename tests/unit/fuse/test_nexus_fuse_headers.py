"""Static checks for the Rust FUSE HTTP client contract."""

from __future__ import annotations

from pathlib import Path


def test_fuse_client_sends_server_agent_header_name() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    client_source = (repo_root / "nexus-fuse" / "src" / "client.rs").read_text(encoding="utf-8")

    assert "X-Agent-ID" in client_source
    assert "X-Nexus-Agent-Id" not in client_source


def test_fuse_fs_uses_open_handle_cache_for_range_reads() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fs_source = (repo_root / "nexus-fuse" / "src" / "fs.rs").read_text(encoding="utf-8")

    assert "open_file_cache" in fs_source
    assert "reply.opened(fh, 0)" in fs_source
    assert "Self::reply_data_slice(&entry.content, offset, size, reply)" in fs_source


def test_fuse_open_handle_cache_has_byte_budget() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fs_source = (repo_root / "nexus-fuse" / "src" / "fs.rs").read_text(encoding="utf-8")

    assert "MAX_OPEN_FILE_CACHE_BYTES" in fs_source
    assert "total_bytes" in fs_source
    assert "content.len() <= MAX_OPEN_FILE_CACHE_BYTES" in fs_source
    assert "content.len() > self.max_bytes" in fs_source
    assert ".pop_lru()" in fs_source
