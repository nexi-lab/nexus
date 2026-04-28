"""Unit tests for ConflictDetector — edge split-brain conflict resolution.

Issue #1707: Edge split-brain resilience.
"""

from nexus.proxy.conflict_detector import (
    ConflictDetector,
    ConflictOutcome,
    OperationState,
)
from nexus.proxy.vector_clock import VectorClock


class TestConflictDetectorNoConflict:
    """Cases where no conflict exists."""

    def test_equal_clocks_no_conflict(self) -> None:
        detector = ConflictDetector(node_id="test-edge")
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id="abc123",
            timestamp=100.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id="abc123",
            timestamp=100.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.NO_CONFLICT
        assert "identical" in result.reason

    def test_concurrent_same_content_id_no_conflict(self) -> None:
        """Concurrent clocks but same content → no conflict."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 2, "cloud": 1}),
            content_id="same-hash",
            timestamp=100.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id="same-hash",
            timestamp=200.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.NO_CONFLICT
        assert "content_id" in result.reason


class TestConflictDetectorEdgeWins:
    """Cases where edge operation should be applied."""

    def test_edge_after_cloud(self) -> None:
        """Edge happened-after cloud → edge wins."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 3, "cloud": 2}),
            timestamp=200.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            timestamp=100.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.EDGE_WINS
        assert "happened-after" in result.reason

    def test_concurrent_lww_edge_later(self) -> None:
        """Concurrent clocks, edge has later timestamp → edge wins (LWW)."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 2, "cloud": 1}),
            content_id="edge-hash",
            timestamp=200.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id="cloud-hash",
            timestamp=100.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.EDGE_WINS
        assert "LWW" in result.reason


class TestConflictDetectorCloudWins:
    """Cases where cloud operation should be applied."""

    def test_edge_before_cloud(self) -> None:
        """Edge happened-before cloud → cloud wins."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 1}),
            timestamp=100.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 2, "cloud": 3}),
            timestamp=200.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.CLOUD_WINS
        assert "happened-before" in result.reason

    def test_concurrent_lww_cloud_later(self) -> None:
        """Concurrent clocks, cloud has later timestamp → cloud wins (LWW)."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 2, "cloud": 1}),
            content_id="edge-hash",
            timestamp=100.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id="cloud-hash",
            timestamp=200.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.CLOUD_WINS
        assert "LWW" in result.reason


class TestConflictDetectorTrueConflict:
    """Cases where true conflict requires manual resolution."""

    def test_concurrent_equal_timestamps(self) -> None:
        """Concurrent clocks + equal timestamps → true conflict."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 2, "cloud": 1}),
            content_id="edge-hash",
            timestamp=100.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id="cloud-hash",
            timestamp=100.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.TRUE_CONFLICT
        assert "equal timestamps" in result.reason

    def test_result_carries_both_states(self) -> None:
        """ConflictResult stores both edge and cloud state for auditing."""
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 2}),
            content_id="e",
            timestamp=100.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"cloud": 2}),
            content_id="c",
            timestamp=100.0,
        )
        result = detector.detect(edge, cloud)
        assert result.edge_state is edge
        assert result.cloud_state is cloud


class TestConflictDetectorNoneContentId:
    """Content_id comparison is skipped when either content_id is None."""

    def test_none_content_ids_use_lww(self) -> None:
        detector = ConflictDetector()
        edge = OperationState(
            vector_clock=VectorClock(counters={"edge": 2, "cloud": 1}),
            content_id=None,
            timestamp=200.0,
        )
        cloud = OperationState(
            vector_clock=VectorClock(counters={"edge": 1, "cloud": 2}),
            content_id=None,
            timestamp=100.0,
        )
        result = detector.detect(edge, cloud)
        assert result.outcome is ConflictOutcome.EDGE_WINS
