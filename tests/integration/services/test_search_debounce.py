"""Tests for search daemon subtree coalescing (Issue #3148, Decision #13B).

Verifies that burst file writes during sync are coalesced into
directory-level index operations.
"""

from nexus.bricks.search.daemon import SearchDaemon


class TestCoalesceSubtrees:
    """Test the _coalesce_subtrees static method."""

    def test_below_threshold_no_coalescing(self) -> None:
        """Small number of paths returned as-is."""
        paths = {"/mnt/gmail/INBOX/1.yaml", "/mnt/gmail/INBOX/2.yaml"}
        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)
        assert set(result) == paths

    def test_above_threshold_coalesces_to_parent(self) -> None:
        """Many paths under same parent are replaced with parent dir."""
        paths = {f"/mnt/gmail/INBOX/{i}.yaml" for i in range(30)}
        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)
        assert "/mnt/gmail/INBOX" in result
        assert len(result) == 1  # All coalesced into one

    def test_mixed_directories(self) -> None:
        """Paths from multiple dirs — only large groups coalesced."""
        paths = set()
        # 25 paths under INBOX (above threshold)
        for i in range(25):
            paths.add(f"/mnt/gmail/INBOX/{i}.yaml")
        # 5 paths under SENT (below threshold)
        for i in range(5):
            paths.add(f"/mnt/gmail/SENT/{i}.yaml")

        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)

        # INBOX should be coalesced
        assert "/mnt/gmail/INBOX" in result
        # SENT paths should remain individual
        sent_paths = [p for p in result if "/SENT/" in p]
        assert len(sent_paths) == 5

    def test_exact_threshold(self) -> None:
        """Exactly at threshold triggers coalescing."""
        paths = {f"/mnt/drive/docs/{i}.md" for i in range(20)}
        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)
        assert "/mnt/drive/docs" in result
        assert len(result) == 1

    def test_empty_paths(self) -> None:
        result = SearchDaemon._coalesce_subtrees(set(), threshold=20)
        assert result == []

    def test_single_path(self) -> None:
        paths = {"/mnt/gmail/INBOX/1.yaml"}
        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)
        assert result == ["/mnt/gmail/INBOX/1.yaml"]

    def test_multiple_deep_directories(self) -> None:
        """Coalescing works correctly with deeply nested paths."""
        paths = set()
        for i in range(25):
            paths.add(f"/mnt/drive/shared/team/docs/{i}.md")
        for i in range(25):
            paths.add(f"/mnt/drive/shared/team/images/{i}.png")

        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)
        assert "/mnt/drive/shared/team/docs" in result
        assert "/mnt/drive/shared/team/images" in result
        assert len(result) == 2

    def test_root_level_paths(self) -> None:
        """Paths directly under root dir."""
        paths = {f"/{i}.txt" for i in range(25)}
        result = SearchDaemon._coalesce_subtrees(paths, threshold=20)
        assert "/" in result or "" in result
