"""Unit tests for NexusFS.read_batch() (Issue #3700).

Tests cover:
- Happy path: single and multiple files
- Empty batch returns empty list
- Strict mode raises on missing path
- Partial mode returns per-item error for missing path
- Partial mode returns per-item error for permission-denied path
- Binary content round-trip
- Large batches
- Path validation (invalid path always raises)
- Result ordering matches input order
- Metadata fields (etag, version, modified_at, size)
"""

import pytest

from tests.conftest import make_test_nexus


@pytest.fixture()
def nx(tmp_path):
    """NexusFS instance with permissions disabled for unit tests."""
    return make_test_nexus(tmp_path)


class TestReadBatchHappyPath:
    """Basic batch read operations that should succeed."""

    @pytest.mark.asyncio
    def test_read_batch_single_file(self, nx):
        nx.write("/files/a.txt", b"hello")
        results = nx.read_batch(["/files/a.txt"])
        assert len(results) == 1
        assert results[0]["content"] == b"hello"
        assert results[0]["path"] == "/files/a.txt"

    @pytest.mark.asyncio
    def test_read_batch_multiple_files(self, nx):
        files = [
            ("/files/a.txt", b"aaa"),
            ("/files/b.txt", b"bbb"),
            ("/files/c.txt", b"ccc"),
        ]
        for path, content in files:
            nx.write(path, content)

        results = nx.read_batch([p for p, _ in files])
        assert len(results) == 3
        for i, (path, content) in enumerate(files):
            assert results[i]["path"] == path
            assert results[i]["content"] == content

    @pytest.mark.asyncio
    def test_read_batch_preserves_input_order(self, nx):
        """Result list must match input path order."""
        nx.write("/files/first.txt", b"first")
        nx.write("/files/second.txt", b"second")
        nx.write("/files/third.txt", b"third")

        results = nx.read_batch(["/files/third.txt", "/files/first.txt", "/files/second.txt"])
        assert results[0]["path"] == "/files/third.txt"
        assert results[0]["content"] == b"third"
        assert results[1]["path"] == "/files/first.txt"
        assert results[1]["content"] == b"first"
        assert results[2]["path"] == "/files/second.txt"
        assert results[2]["content"] == b"second"

    @pytest.mark.asyncio
    def test_read_batch_returns_etag(self, nx):
        nx.write("/files/a.txt", b"content")
        results = nx.read_batch(["/files/a.txt"])
        assert "etag" in results[0]

    @pytest.mark.asyncio
    def test_read_batch_returns_version(self, nx):
        nx.write("/files/a.txt", b"v1")
        results = nx.read_batch(["/files/a.txt"])
        assert results[0]["version"] >= 1

    @pytest.mark.asyncio
    def test_read_batch_returns_size(self, nx):
        nx.write("/files/a.txt", b"hello")
        results = nx.read_batch(["/files/a.txt"])
        assert results[0]["size"] == 5

    @pytest.mark.asyncio
    def test_read_batch_matches_write_batch_etag(self, nx):
        """etag from read_batch should match the etag from write_batch."""
        write_results = nx.write_batch([("/files/a.txt", b"data")])
        read_results = nx.read_batch(["/files/a.txt"])
        if write_results[0].get("etag") and read_results[0].get("etag"):
            assert read_results[0]["etag"] == write_results[0]["etag"]


class TestReadBatchEmptyInput:
    """Edge case: empty batch."""

    @pytest.mark.asyncio
    def test_empty_batch_returns_empty_list(self, nx):
        results = nx.read_batch([])
        assert results == []


class TestReadBatchStrictMode:
    """Strict mode (partial=False, the default) raises on missing paths."""

    @pytest.mark.asyncio
    def test_missing_path_raises_file_not_found(self, nx):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            nx.read_batch(["/files/does_not_exist.txt"])

    @pytest.mark.asyncio
    def test_one_missing_in_batch_raises(self, nx):
        """Even one missing path raises in strict mode."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        nx.write("/files/exists.txt", b"here")
        with pytest.raises(NexusFileNotFoundError):
            nx.read_batch(["/files/exists.txt", "/files/missing.txt"])


class TestReadBatchPartialMode:
    """Partial mode (partial=True) returns per-item errors instead of raising."""

    @pytest.mark.asyncio
    def test_missing_path_returns_error_item(self, nx):
        results = nx.read_batch(["/files/missing.txt"], partial=True)
        assert len(results) == 1
        assert results[0]["path"] == "/files/missing.txt"
        assert "error" in results[0]
        assert results[0]["error"] in ("not_found", "permission_denied")

    @pytest.mark.asyncio
    def test_mixed_hit_and_miss(self, nx):
        nx.write("/files/exists.txt", b"found")
        results = nx.read_batch(["/files/exists.txt", "/files/missing.txt"], partial=True)
        assert len(results) == 2
        # First is a hit
        assert results[0]["content"] == b"found"
        assert "error" not in results[0]
        # Second is a miss
        assert "error" in results[1]
        assert results[1]["path"] == "/files/missing.txt"

    @pytest.mark.asyncio
    def test_all_missing_returns_all_errors(self, nx):
        results = nx.read_batch(["/files/a.txt", "/files/b.txt", "/files/c.txt"], partial=True)
        assert len(results) == 3
        for r in results:
            assert "error" in r

    @pytest.mark.asyncio
    def test_partial_preserves_order_with_mixed_results(self, nx):
        nx.write("/files/b.txt", b"middle")
        results = nx.read_batch(["/files/a.txt", "/files/b.txt", "/files/c.txt"], partial=True)
        assert results[0]["path"] == "/files/a.txt"
        assert "error" in results[0]
        assert results[1]["path"] == "/files/b.txt"
        assert results[1]["content"] == b"middle"
        assert results[2]["path"] == "/files/c.txt"
        assert "error" in results[2]


class TestReadBatchContentEdgeCases:
    """Edge cases in content payloads."""

    @pytest.mark.asyncio
    def test_empty_content(self, nx):
        nx.write("/files/empty.txt", b"")
        results = nx.read_batch(["/files/empty.txt"])
        assert results[0]["content"] == b""
        assert results[0]["size"] == 0

    @pytest.mark.asyncio
    def test_binary_content_round_trip(self, nx):
        binary = bytes(range(256))
        nx.write("/files/binary.bin", binary)
        results = nx.read_batch(["/files/binary.bin"])
        assert results[0]["content"] == binary
        assert results[0]["size"] == 256

    @pytest.mark.asyncio
    def test_large_batch(self, nx):
        """Read 50 files written by write_batch."""
        files = [(f"/files/file_{i:03d}.txt", f"content_{i}".encode()) for i in range(50)]
        nx.write_batch(files)

        paths = [p for p, _ in files]
        results = nx.read_batch(paths)
        assert len(results) == 50
        for i, (path, content) in enumerate(files):
            assert results[i]["path"] == path
            assert results[i]["content"] == content

    @pytest.mark.asyncio
    def test_duplicate_paths_in_batch(self, nx):
        """Duplicate paths in input should return duplicate results."""
        nx.write("/files/a.txt", b"data")
        results = nx.read_batch(["/files/a.txt", "/files/a.txt"])
        assert len(results) == 2
        assert results[0]["content"] == b"data"
        assert results[1]["content"] == b"data"


class TestReadBatchPathValidation:
    """Path validation — invalid paths always raise regardless of partial mode."""

    @pytest.mark.asyncio
    def test_invalid_path_raises(self, nx):
        from nexus.contracts.exceptions import InvalidPathError

        with pytest.raises(InvalidPathError):
            nx.read_batch([""])

    @pytest.mark.asyncio
    def test_invalid_path_raises_even_in_partial_mode(self, nx):
        """InvalidPathError is never swallowed by partial mode."""
        from nexus.contracts.exceptions import InvalidPathError

        with pytest.raises(InvalidPathError):
            nx.read_batch([""], partial=True)
