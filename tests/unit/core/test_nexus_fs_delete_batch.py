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
