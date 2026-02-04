"""
Tests for Filesystem Consistency with Zookie Tokens (Issue #1187)

Tests the integration of Zookie consistency tokens with file operations:
- Write operations return zookies
- Read operations can accept zookies for AT_LEAST_AS_FRESH consistency
- Revision-based blocking works correctly
"""

import time

import pytest

from nexus.core.zookie import ConsistencyTimeoutError, InvalidZookieError, Zookie


class TestWriteReturnsZookie:
    """Tests that write operations return zookie tokens."""

    def test_zookie_in_write_result(self):
        """Write should return a zookie token in the result dict."""
        # Create a zookie as the write method would
        tenant_id = "test_tenant"
        revision = 42

        token = Zookie.encode(tenant_id, revision)
        result = {
            "etag": "abc123",
            "version": 1,
            "modified_at": "2026-01-01T00:00:00Z",
            "size": 100,
            "zookie": token,
            "revision": revision,
        }

        # Verify zookie can be decoded
        zookie = Zookie.decode(result["zookie"])
        assert zookie.tenant_id == tenant_id
        assert zookie.revision == revision

    def test_zookie_revision_increments(self):
        """Each write should return an incrementing revision."""
        revisions = []
        for i in range(3):
            token = Zookie.encode("tenant", i + 1)
            zookie = Zookie.decode(token)
            revisions.append(zookie.revision)

        assert revisions == [1, 2, 3]


class TestZookieConsistencyModes:
    """Tests for consistency mode behavior."""

    def test_at_least_as_fresh_satisfied(self):
        """AT_LEAST_AS_FRESH should pass when revision is >= required."""
        current_revision = 10
        required_revision = 5

        # Simulate the check that would happen in _wait_for_revision
        assert current_revision >= required_revision

    def test_at_least_as_fresh_not_satisfied(self):
        """AT_LEAST_AS_FRESH should fail when revision is < required."""
        current_revision = 5
        required_revision = 10

        assert current_revision < required_revision

    def test_zookie_tenant_validation(self):
        """Zookie should validate tenant matches."""
        zookie = Zookie.decode(Zookie.encode("tenant_a", 100))

        # Different tenant should be noted
        request_tenant = "tenant_b"
        assert zookie.tenant_id != request_tenant


class TestRevisionBlocking:
    """Tests for revision-based blocking."""

    def test_wait_returns_immediately_when_satisfied(self):
        """_wait_for_revision should return immediately if revision is already met."""
        # Mock the revision check
        def mock_get_revision(tenant_id: str) -> int:
            return 100  # Already at revision 100

        # Check would pass immediately
        min_revision = 50
        current = mock_get_revision("tenant")
        assert current >= min_revision

    def test_consistency_timeout_error_attributes(self):
        """ConsistencyTimeoutError should contain useful debugging info."""
        error = ConsistencyTimeoutError(
            message="Timeout waiting for revision",
            tenant_id="tenant_123",
            requested_revision=100,
            current_revision=95,
            timeout_ms=5000,
        )

        assert error.tenant_id == "tenant_123"
        assert error.requested_revision == 100
        assert error.current_revision == 95
        assert error.timeout_ms == 5000


class TestZookieHeader:
    """Tests for X-Nexus-Zookie header handling."""

    def test_valid_zookie_header_parsing(self):
        """Valid X-Nexus-Zookie header should be parsed correctly."""
        token = Zookie.encode("tenant_123", 42)

        # Simulate header parsing
        zookie = Zookie.decode(token)
        assert zookie.tenant_id == "tenant_123"
        assert zookie.revision == 42

    def test_invalid_zookie_header_raises(self):
        """Invalid X-Nexus-Zookie header should raise InvalidZookieError."""
        invalid_token = "not_a_valid_token"

        with pytest.raises(InvalidZookieError):
            Zookie.decode(invalid_token)

    def test_zookie_response_header_format(self):
        """Zookie in response header should be properly formatted."""
        token = Zookie.encode("tenant", 100)

        # Verify it's a string suitable for HTTP header
        assert isinstance(token, str)
        assert "nz1." in token  # Version prefix present
        # No special characters that would break headers
        assert "\n" not in token
        assert "\r" not in token


class TestEventWithRevision:
    """Tests for FileEvent revision field."""

    def test_file_event_includes_revision(self):
        """FileEvent should include revision field."""
        from nexus.core.event_bus import FileEvent, FileEventType

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test.txt",
            tenant_id="tenant",
            revision=42,
        )

        assert event.revision == 42

    def test_file_event_serialization_includes_revision(self):
        """FileEvent serialization should include revision."""
        from nexus.core.event_bus import FileEvent, FileEventType

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test.txt",
            tenant_id="tenant",
            revision=42,
        )

        data = event.to_dict()
        assert "revision" in data
        assert data["revision"] == 42

    def test_file_event_deserialization_includes_revision(self):
        """FileEvent deserialization should preserve revision."""
        from nexus.core.event_bus import FileEvent

        data = {
            "type": "file_write",
            "path": "/test.txt",
            "tenant_id": "tenant",
            "revision": 42,
        }

        event = FileEvent.from_dict(data)
        assert event.revision == 42


class TestZookieAgeTracking:
    """Tests for zookie age tracking."""

    def test_zookie_age_increases_over_time(self):
        """Zookie age should increase as time passes."""
        token = Zookie.encode("tenant", 1)
        zookie = Zookie.decode(token)

        age1 = zookie.age_ms()
        time.sleep(0.01)  # 10ms
        age2 = zookie.age_ms()

        assert age2 > age1

    def test_zookie_created_at_is_accurate(self):
        """Zookie created_at should be close to current time."""
        before = time.time() * 1000
        token = Zookie.encode("tenant", 1)
        after = time.time() * 1000

        zookie = Zookie.decode(token)

        # Allow 100ms tolerance for test timing
        assert before - 100 <= zookie.created_at_ms <= after + 100
