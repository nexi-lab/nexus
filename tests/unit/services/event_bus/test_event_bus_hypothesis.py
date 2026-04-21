"""Property-based tests for EventBus using Hypothesis."""

import pytest

pytest.importorskip("hypothesis")

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import path_matches_pattern
from nexus.services.event_bus.types import FileEvent, FileEventType

# Strategy for generating valid file paths
valid_paths = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # Exclude surrogates
        blacklist_characters="\x00\n\r\t",  # Exclude null bytes and control chars
    ),
    min_size=1,
    max_size=200,
).filter(lambda s: len(s.strip()) > 0)  # Exclude whitespace-only


# Strategy for generating event types
event_types = st.sampled_from(
    [
        FileEventType.FILE_WRITE,
        FileEventType.FILE_DELETE,
        FileEventType.FILE_RENAME,
        FileEventType.METADATA_CHANGE,
        FileEventType.DIR_CREATE,
        FileEventType.DIR_DELETE,
    ]
)


# Strategy for generating glob patterns
glob_patterns = st.sampled_from(
    [
        "/inbox/*",
        "/inbox/**",
        "*.txt",
        "/test.txt",
        "**/*.py",
        "/data/**/*.json",
        "/*",
        "/**",
    ]
)


class TestPatternMatchingProperties:
    """Property-based tests for path pattern matching."""

    @given(path=valid_paths, pattern=glob_patterns)
    @settings(max_examples=100)
    def test_pattern_matching_is_deterministic(self, path, pattern):
        """Property: Pattern matching is deterministic (same input → same output)."""
        result1 = path_matches_pattern(path, pattern)
        result2 = path_matches_pattern(path, pattern)
        assert result1 == result2, f"Non-deterministic for path={path!r}, pattern={pattern!r}"

    @given(path=valid_paths)
    @settings(max_examples=50)
    def test_exact_match_pattern(self, path):
        """Property: Exact path always matches itself."""
        # Path should match itself exactly
        assert path_matches_pattern(path, path) is True

    @given(path=valid_paths)
    @settings(max_examples=50)
    def test_wildcard_matches_everything(self, path):
        """Property: ** pattern matches all paths."""
        # Universal wildcard should match any path
        assert path_matches_pattern(path, "**") is True

    @given(path=valid_paths)
    @settings(max_examples=50)
    def test_pattern_matching_is_boolean(self, path):
        """Property: Pattern matching always returns bool."""
        patterns = ["/inbox/*", "**/*.txt", path, "/**"]
        for pattern in patterns:
            result = path_matches_pattern(path, pattern)
            assert isinstance(result, bool), f"Non-boolean result for {path!r}, {pattern!r}"


class TestFileEventProperties:
    """Property-based tests for FileEvent."""

    @given(
        event_type=event_types,
        path=valid_paths,
        zone_id=st.one_of(st.none(), st.text(min_size=1, max_size=50)),
    )
    @settings(max_examples=100)
    def test_event_creation_is_idempotent(self, event_type, path, zone_id):
        """Property: Creating same event multiple times yields equal objects."""
        event1 = FileEvent(type=event_type, path=path, zone_id=zone_id, event_id="test-id")
        event2 = FileEvent(type=event_type, path=path, zone_id=zone_id, event_id="test-id")

        # Events with same event_id should be equal
        assert event1 == event2
        assert hash(event1) == hash(event2)

    @given(
        event_type=event_types,
        path=valid_paths,
        zone_id=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=100)
    def test_event_serialization_roundtrip(self, event_type, path, zone_id):
        """Property: Serialization round-trip preserves event data."""
        original = FileEvent(type=event_type, path=path, zone_id=zone_id)

        # Serialize and deserialize
        json_str = original.to_json()
        restored = FileEvent.from_json(json_str)

        # Key fields should match
        assert restored.type == original.type
        assert restored.path == original.path
        assert restored.zone_id == original.zone_id
        assert restored.event_id == original.event_id

    @given(
        event_type=event_types,
        path=valid_paths,
        zone_id=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=50)
    def test_event_dict_roundtrip(self, event_type, path, zone_id):
        """Property: Dict conversion round-trip preserves data."""
        original = FileEvent(type=event_type, path=path, zone_id=zone_id)

        # Convert to dict and back
        data = original.to_dict()
        restored = FileEvent.from_dict(data)

        assert restored.type == original.type
        assert restored.path == original.path
        assert restored.zone_id == original.zone_id

    @given(
        events=st.lists(
            st.builds(
                FileEvent,
                type=event_types,
                path=valid_paths,
                zone_id=st.just("root"),
            ),
            min_size=0,
            max_size=50,
        )
    )
    @settings(max_examples=50)
    def test_event_id_uniqueness(self, events):
        """Property: Auto-generated event IDs are unique."""
        event_ids = [event.event_id for event in events]

        # All event IDs should be unique (with high probability)
        if len(events) > 1:
            assert len(set(event_ids)) == len(event_ids)


class TestRevisionOrderingProperties:
    """Property-based tests for revision-based ordering."""

    @given(
        revisions=st.lists(
            st.integers(min_value=0, max_value=10000),
            min_size=1,
            max_size=100,
        )
    )
    @settings(max_examples=50)
    def test_revision_ordering_is_transitive(self, revisions):
        """Property: If A < B and B < C, then A < C."""
        sorted_revisions = sorted(revisions)

        for i in range(len(sorted_revisions) - 2):
            a, b, c = sorted_revisions[i], sorted_revisions[i + 1], sorted_revisions[i + 2]
            if a < b and b < c:
                assert a < c, f"Transitivity violated: {a} < {b} < {c}"


class TestPathMatchingProperties:
    """Advanced property-based tests for path pattern matching."""

    @given(path=valid_paths, pattern=valid_paths)
    @settings(max_examples=100)
    def test_exact_path_matches_self(self, path, pattern):
        """Property: A path always matches itself as a pattern."""
        if path == pattern:
            assert path_matches_pattern(path, pattern) is True

    @given(
        base_path=st.text(
            min_size=1, max_size=50, alphabet=st.characters(min_codepoint=97, max_codepoint=122)
        ),
        filename=st.text(
            min_size=1, max_size=20, alphabet=st.characters(min_codepoint=97, max_codepoint=122)
        ),
    )
    @settings(max_examples=50)
    def test_wildcard_pattern_matching(self, base_path, filename):
        """Property: /path/* matches /path/file."""
        full_path = f"/{base_path}/{filename}"
        pattern = f"/{base_path}/*"

        result = path_matches_pattern(full_path, pattern)
        # Should match if filename doesn't contain /
        if "/" not in filename:
            assert result is True

    @given(path=valid_paths)
    @settings(max_examples=50)
    def test_empty_pattern_never_matches(self, path):
        """Property: Empty pattern should not match (unless path is also empty)."""
        assume(len(path) > 0)
        # Empty patterns shouldn't match non-empty paths
        # Note: This depends on implementation details
        result = path_matches_pattern(path, "")
        assert result is False


class TestEventMatchingProperties:
    """Property-based tests for event.matches_path_pattern()."""

    @given(path=valid_paths)
    @settings(max_examples=50)
    def test_event_matches_own_path(self, path):
        """Property: Event matches pattern equal to its own path."""
        event = FileEvent(type=FileEventType.FILE_WRITE, path=path, zone_id=ROOT_ZONE_ID)
        assert event.matches_path_pattern(path) is True

    @given(
        event_path=valid_paths,
        old_path=valid_paths,
        pattern=glob_patterns,
    )
    @settings(max_examples=100)
    def test_rename_event_matches_both_paths(self, event_path, old_path, pattern):
        """Property: Rename events match pattern if either path matches."""
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path=event_path,
            old_path=old_path,
            zone_id=ROOT_ZONE_ID,
        )

        result = event.matches_path_pattern(pattern)

        # If either path matches, event should match
        path_match = path_matches_pattern(event_path, pattern)
        old_path_match = path_matches_pattern(old_path, pattern)

        if path_match or old_path_match:
            assert result is True
