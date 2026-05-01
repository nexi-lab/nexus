"""Tests for local archive storage backend."""

from datetime import datetime

from nexus.bricks.archive.storage.local import LocalArchiveStorage


def test_put_then_list(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("daily/a.nexus", src)
    listed = storage.list("daily/")
    assert any(e.key == "daily/a.nexus" for e in listed)


def test_list_returns_size_and_mtime(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "a.nexus"
    src.write_bytes(b"hello")
    storage.put("a.nexus", src)
    entries = storage.list("")
    e = next(e for e in entries if e.key == "a.nexus")
    assert e.size_bytes == 5
    assert isinstance(e.last_modified, datetime)


def test_delete_removes_file(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("a.nexus", src)
    storage.delete("a.nexus")
    assert storage.list("") == []


def test_get_writes_to_target(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "src.nexus"
    src.write_bytes(b"contents")
    storage.put("a.nexus", src)
    target = tmp_path / "downloaded.nexus"
    storage.get("a.nexus", target)
    assert target.read_bytes() == b"contents"
