"""Regression tests for NexusFS.delete_batch() — Issue #4002.

Covers:
- Result keys preserve the caller's literal request path (no leading-slash
  rewrite), matching write_batch / exists_batch / read_bulk shape.
- Files written via write_batch are actually deleted (no divergence between
  the existence check inside delete_batch and the rest of the API).
- Missing files report "File not found" under the literal request key.
- Mixed present/missing batches return one entry per input path.
"""

import pytest

from tests.conftest import make_test_nexus


@pytest.fixture()
def nx(tmp_path):
    return make_test_nexus(tmp_path)


class TestDeleteBatchRoundTrip:
    def test_write_then_delete_then_exists(self, nx):
        path = "delete-test-fresh.json"
        nx.write_batch([(path, b"hello")])
        assert nx.exists_batch([path]) == {path: True}

        result = nx.delete_batch([path])

        assert result == {path: {"success": True}}
        assert nx.exists_batch([path]) == {path: False}

    def test_response_key_matches_request_key_without_leading_slash(self, nx):
        path = "no-slash.json"
        nx.write_batch([(path, b"x")])

        result = nx.delete_batch([path])

        assert list(result.keys()) == [path]
        assert "/" + path not in result

    def test_response_key_matches_request_key_with_leading_slash(self, nx):
        path = "/with-slash.json"
        nx.write_batch([(path, b"x")])

        result = nx.delete_batch([path])

        assert list(result.keys()) == [path]


class TestDeleteBatchMissing:
    def test_missing_file_reports_not_found_under_request_key(self, nx):
        path = "never-existed.json"

        result = nx.delete_batch([path])

        assert result == {path: {"success": False, "error": "File not found"}}

    def test_mixed_present_and_missing(self, nx):
        present = "exists.json"
        missing = "ghost.json"
        nx.write_batch([(present, b"data")])

        result = nx.delete_batch([present, missing])

        assert result[present] == {"success": True}
        assert result[missing] == {"success": False, "error": "File not found"}
        assert set(result.keys()) == {present, missing}


class TestDeleteBatchEmpty:
    def test_empty_input_returns_empty_dict(self, nx):
        assert nx.delete_batch([]) == {}


class TestDeleteBatchImplicitDirectory:
    """Codex review: implicit directories (paths with children but no
    explicit inode) must be deletable through delete_batch when recursive."""

    def test_implicit_dir_recursive_delete(self, nx):
        nx.write_batch(
            [
                ("/parent/child1.txt", b"a"),
                ("/parent/child2.txt", b"b"),
            ]
        )
        assert nx.exists_batch(["/parent"]) == {"/parent": True}

        result = nx.delete_batch(["/parent"], recursive=True)

        assert result == {"/parent": {"success": True}}
        assert nx.exists_batch(["/parent/child1.txt", "/parent/child2.txt"]) == {
            "/parent/child1.txt": False,
            "/parent/child2.txt": False,
        }

    def test_implicit_dir_non_recursive_reports_not_empty(self, nx):
        nx.write_batch([("/parent/child.txt", b"a")])

        result = nx.delete_batch(["/parent"])

        assert result["/parent"]["success"] is False
        assert "not empty" in result["/parent"]["error"].lower()
        assert nx.exists_batch(["/parent/child.txt"]) == {"/parent/child.txt": True}


class _NonHitResult:
    hit = False
    entry_type = 99  # not 0 (not-found) and not 5 (external storage)
    post_hook_needed = False


class _NonHitKernelWrapper:
    """Wraps the real kernel but forces sys_unlink to return a non-hit
    with a nonzero entry_type — simulates write-lock timeout / internal
    abort that Codex flagged as a silent-success path."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sys_unlink(self, *_args, **_kwargs):
        return _NonHitResult()


class TestSysUnlinkNonHit:
    """Codex review: sys_unlink must surface non-hit kernel results so
    delete_batch records a real failure instead of phantom success."""

    def test_unknown_entry_type_raises(self, nx, monkeypatch):
        from nexus.contracts.exceptions import BackendError

        nx.write_batch([("/lock-timeout.txt", b"x")])
        monkeypatch.setattr(nx, "_kernel", _NonHitKernelWrapper(nx._kernel))

        with pytest.raises(BackendError, match="entry_type=99"):
            nx.sys_unlink("/lock-timeout.txt")

    def test_delete_batch_reports_failure_on_non_hit(self, nx, monkeypatch):
        nx.write_batch([("/lock-timeout.txt", b"x")])
        monkeypatch.setattr(nx, "_kernel", _NonHitKernelWrapper(nx._kernel))

        result = nx.delete_batch(["/lock-timeout.txt"])

        assert result["/lock-timeout.txt"]["success"] is False
        assert "entry_type=99" in result["/lock-timeout.txt"]["error"]
