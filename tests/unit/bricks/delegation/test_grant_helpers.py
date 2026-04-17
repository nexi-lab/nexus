"""Unit tests for grant_helpers: GrantInput + grants_to_rebac_tuples (Issue #3130)."""

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.grant_helpers import (
    MAX_REGISTRATION_GRANTS,
    GrantInput,
    grants_to_rebac_tuples,
    validate_grant,
)


class TestValidateGrant:
    """Tests for validate_grant()."""

    def test_valid_editor_grant(self):
        """Editor grants should pass validation."""
        grant = GrantInput(path="/workspace/main.py", role="editor")
        validate_grant(grant)  # Should not raise

    def test_valid_viewer_grant(self):
        """Viewer grants should pass validation."""
        grant = GrantInput(path="/workspace/docs/*", role="viewer")
        validate_grant(grant)  # Should not raise

    def test_reject_deny_role(self):
        """'deny' role must be rejected with a clear error."""
        grant = GrantInput(path="/workspace/secrets/*", role="deny")
        with pytest.raises(ValueError, match="Invalid role 'deny'"):
            validate_grant(grant)

    def test_reject_unknown_role(self):
        """Unknown roles must be rejected."""
        grant = GrantInput(path="/workspace/", role="owner")
        with pytest.raises(ValueError, match="Invalid role 'owner'"):
            validate_grant(grant)

    def test_reject_path_without_leading_slash(self):
        """Paths must start with '/'."""
        grant = GrantInput(path="workspace/file.py", role="editor")
        with pytest.raises(ValueError, match="must start with '/'"):
            validate_grant(grant)

    def test_reject_path_traversal_dotdot(self):
        """Paths containing '..' must be rejected."""
        grant = GrantInput(path="/workspace/../etc/passwd", role="editor")
        with pytest.raises(ValueError, match="must not contain '..' traversal"):
            validate_grant(grant)

    def test_reject_path_traversal_leading_dotdot(self):
        """Leading '..' in path components must be rejected."""
        grant = GrantInput(path="/../../../etc/shadow", role="editor")
        with pytest.raises(ValueError, match="must not contain '..' traversal"):
            validate_grant(grant)

    def test_reject_path_traversal_trailing_dotdot(self):
        """Trailing '..' must be rejected."""
        grant = GrantInput(path="/workspace/..", role="editor")
        with pytest.raises(ValueError, match="must not contain '..' traversal"):
            validate_grant(grant)

    def test_allow_double_dot_in_filename(self):
        """Double dots in filenames (not as path component) are valid."""
        grant = GrantInput(path="/workspace/file..txt", role="editor")
        validate_grant(grant)  # Should not raise — '..txt' is not traversal

    def test_allow_glob_patterns(self):
        """Glob patterns like * and ** should be accepted."""
        grant = GrantInput(path="/workspace/**/*.py", role="viewer")
        validate_grant(grant)  # Should not raise


class TestGrantsToRebacTuples:
    """Tests for grants_to_rebac_tuples()."""

    def test_empty_grants_returns_empty(self):
        """Empty grant list should return empty tuple list."""
        result = grants_to_rebac_tuples([], agent_id="agent-1")
        assert result == []

    def test_editor_maps_to_direct_editor(self):
        """'editor' role must map to 'direct_editor' relation."""
        grants = [GrantInput(path="/workspace/file.py", role="editor")]
        tuples = grants_to_rebac_tuples(grants, agent_id="agent-1", zone_id=ROOT_ZONE_ID)

        assert len(tuples) == 1
        assert tuples[0]["subject"] == ("agent", "agent-1")
        assert tuples[0]["relation"] == "direct_editor"
        assert tuples[0]["object"] == ("file", "/workspace/file.py")
        assert tuples[0]["zone_id"] == "root"

    def test_viewer_maps_to_direct_viewer(self):
        """'viewer' role must map to 'direct_viewer' relation."""
        grants = [GrantInput(path="/docs/*", role="viewer")]
        tuples = grants_to_rebac_tuples(grants, agent_id="agent-2", zone_id="z1")

        assert len(tuples) == 1
        assert tuples[0]["relation"] == "direct_viewer"

    def test_multiple_grants(self):
        """Multiple grants should produce multiple tuples."""
        grants = [
            GrantInput(path="/workspace/*", role="editor"),
            GrantInput(path="/logs/*", role="viewer"),
            GrantInput(path="/config/app.json", role="viewer"),
        ]
        tuples = grants_to_rebac_tuples(grants, agent_id="multi-agent", zone_id=ROOT_ZONE_ID)

        assert len(tuples) == 3
        assert tuples[0]["relation"] == "direct_editor"
        assert tuples[1]["relation"] == "direct_viewer"
        assert tuples[2]["object"] == ("file", "/config/app.json")

    def test_default_zone_id(self):
        """zone_id should default to ROOT_ZONE_ID when not provided."""
        from nexus.contracts.constants import ROOT_ZONE_ID

        grants = [GrantInput(path="/workspace/", role="editor")]
        tuples = grants_to_rebac_tuples(grants, agent_id="agent-3")

        assert tuples[0]["zone_id"] == ROOT_ZONE_ID

    def test_exceeds_max_grants_raises(self):
        """Exceeding MAX_REGISTRATION_GRANTS must raise ValueError."""
        grants = [
            GrantInput(path=f"/path/{i}", role="editor") for i in range(MAX_REGISTRATION_GRANTS + 1)
        ]
        with pytest.raises(ValueError, match="Too many grants"):
            grants_to_rebac_tuples(grants, agent_id="agent-4")

    def test_exactly_max_grants_allowed(self):
        """Exactly MAX_REGISTRATION_GRANTS should be accepted."""
        grants = [
            GrantInput(path=f"/path/{i}", role="editor") for i in range(MAX_REGISTRATION_GRANTS)
        ]
        tuples = grants_to_rebac_tuples(grants, agent_id="agent-5")
        assert len(tuples) == MAX_REGISTRATION_GRANTS

    def test_invalid_grant_in_list_raises(self):
        """If any grant in the list is invalid, the whole call should fail."""
        grants = [
            GrantInput(path="/workspace/ok.py", role="editor"),
            GrantInput(path="/workspace/bad", role="deny"),
        ]
        with pytest.raises(ValueError, match="Invalid role 'deny'"):
            grants_to_rebac_tuples(grants, agent_id="agent-6")
