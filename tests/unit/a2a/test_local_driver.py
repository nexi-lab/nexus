"""Tests for LocalStorageDriver (filesystem-backed IPCStorageDriver).

Verifies async file operations, path traversal prevention, and
correct behavior for all IPCStorageDriver protocol methods.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.a2a.stores.local_driver import LocalStorageDriver


@pytest.fixture()
def driver(tmp_path):
    """LocalStorageDriver rooted in a temp directory."""
    return LocalStorageDriver(root=tmp_path)


class TestLocalStorageDriverRead:
    def test_read_existing_file(self, driver, tmp_path):
        (tmp_path / "hello.txt").write_bytes(b"world")
        result = asyncio.get_event_loop().run_until_complete(driver.read("/hello.txt", zone_id="z"))
        assert result == b"world"

    def test_read_missing_file_raises(self, driver):
        with pytest.raises(FileNotFoundError):
            asyncio.get_event_loop().run_until_complete(
                driver.read("/no-such-file.txt", zone_id="z")
            )


class TestLocalStorageDriverWrite:
    def test_write_creates_file(self, driver, tmp_path):
        asyncio.get_event_loop().run_until_complete(driver.write("/data.bin", b"abc", zone_id="z"))
        assert (tmp_path / "data.bin").read_bytes() == b"abc"

    def test_write_creates_parent_dirs(self, driver, tmp_path):
        asyncio.get_event_loop().run_until_complete(
            driver.write("/a/b/c.txt", b"deep", zone_id="z")
        )
        assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"deep"

    def test_write_overwrites_existing(self, driver, tmp_path):
        asyncio.get_event_loop().run_until_complete(driver.write("/f.txt", b"v1", zone_id="z"))
        asyncio.get_event_loop().run_until_complete(driver.write("/f.txt", b"v2", zone_id="z"))
        assert (tmp_path / "f.txt").read_bytes() == b"v2"


class TestLocalStorageDriverListDir:
    def test_list_dir_returns_entries(self, driver, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"a")
        (tmp_path / "sub" / "b.txt").write_bytes(b"b")
        entries = asyncio.get_event_loop().run_until_complete(driver.list_dir("/sub", zone_id="z"))
        assert sorted(entries) == ["a.txt", "b.txt"]

    def test_list_dir_missing_raises(self, driver):
        with pytest.raises(FileNotFoundError):
            asyncio.get_event_loop().run_until_complete(driver.list_dir("/no-dir", zone_id="z"))


class TestLocalStorageDriverCountDir:
    def test_count_dir(self, driver, tmp_path):
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "x.json").write_bytes(b"{}")
        (tmp_path / "d" / "y.json").write_bytes(b"{}")
        count = asyncio.get_event_loop().run_until_complete(driver.count_dir("/d", zone_id="z"))
        assert count == 2


class TestLocalStorageDriverRename:
    def test_rename_moves_file(self, driver, tmp_path):
        (tmp_path / "old.txt").write_bytes(b"data")
        asyncio.get_event_loop().run_until_complete(
            driver.rename("/old.txt", "/new.txt", zone_id="z")
        )
        assert not (tmp_path / "old.txt").exists()
        assert (tmp_path / "new.txt").read_bytes() == b"data"

    def test_rename_missing_raises(self, driver):
        with pytest.raises(FileNotFoundError):
            asyncio.get_event_loop().run_until_complete(
                driver.rename("/missing.txt", "/dest.txt", zone_id="z")
            )


class TestLocalStorageDriverMkdir:
    def test_mkdir_creates_dirs(self, driver, tmp_path):
        asyncio.get_event_loop().run_until_complete(driver.mkdir("/p/q/r", zone_id="z"))
        assert (tmp_path / "p" / "q" / "r").is_dir()


class TestLocalStorageDriverExists:
    def test_exists_true(self, driver, tmp_path):
        (tmp_path / "there.txt").write_bytes(b"hi")
        result = asyncio.get_event_loop().run_until_complete(
            driver.exists("/there.txt", zone_id="z")
        )
        assert result is True

    def test_exists_false(self, driver):
        result = asyncio.get_event_loop().run_until_complete(
            driver.exists("/nope.txt", zone_id="z")
        )
        assert result is False


class TestPathTraversalPrevention:
    def test_path_traversal_blocked(self, driver):
        with pytest.raises(ValueError, match="Path traversal blocked"):
            asyncio.get_event_loop().run_until_complete(
                driver.read("/../../../etc/passwd", zone_id="z")
            )

    def test_dotdot_in_middle_blocked(self, driver):
        with pytest.raises(ValueError, match="Path traversal blocked"):
            asyncio.get_event_loop().run_until_complete(
                driver.read("/a/../../etc/passwd", zone_id="z")
            )
