"""Tests for Subsystem ABC, ContextIdentity, and extract_context_identity.

Issue #1287: Extract NexusFS Domain Services from God Object (Phase A).
"""

from __future__ import annotations

import pytest

from nexus.services.subsystem import ContextIdentity, extract_context_identity


class TestContextIdentity:
    """Tests for ContextIdentity dataclass."""

    def test_frozen(self) -> None:
        identity = ContextIdentity(zone_id="z1", user_id="u1", is_admin=False)
        with pytest.raises(AttributeError):
            identity.zone_id = "z2"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = ContextIdentity(zone_id="z1", user_id="u1", is_admin=True)
        b = ContextIdentity(zone_id="z1", user_id="u1", is_admin=True)
        assert a == b

    def test_inequality(self) -> None:
        a = ContextIdentity(zone_id="z1", user_id="u1", is_admin=True)
        b = ContextIdentity(zone_id="z2", user_id="u1", is_admin=True)
        assert a != b


class TestExtractContextIdentity:
    """Tests for extract_context_identity() helper."""

    def test_none_context_returns_defaults(self) -> None:
        identity = extract_context_identity(None)
        assert identity.zone_id == "default"
        assert identity.user_id == "anonymous"
        assert identity.is_admin is False

    def test_extracts_from_operation_context(self) -> None:
        from nexus.core.permissions import OperationContext

        ctx = OperationContext(
            user="alice",
            groups=["devs"],
            zone_id="org_acme",
            is_admin=True,
        )
        identity = extract_context_identity(ctx)
        assert identity.zone_id == "org_acme"
        assert identity.user_id == "alice"
        assert identity.is_admin is True

    def test_none_zone_id_defaults_to_default(self) -> None:
        from nexus.core.permissions import OperationContext

        ctx = OperationContext(user="bob", groups=[], zone_id=None)
        identity = extract_context_identity(ctx)
        assert identity.zone_id == "default"

    def test_subject_id_used_when_available(self) -> None:
        """subject_id is accessible on OperationContext."""
        from nexus.core.permissions import OperationContext

        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_id="agent_007",
        )
        identity = extract_context_identity(ctx)
        # user field takes precedence in extract_context_identity
        assert identity.user_id == "alice"

    def test_duck_typed_context(self) -> None:
        """extract_context_identity works with any duck-typed context object."""
        from types import SimpleNamespace

        ctx = SimpleNamespace(zone_id="z1", user=None, subject_id="svc_001", is_admin=False)
        identity = extract_context_identity(ctx)  # type: ignore[arg-type]
        assert identity.user_id == "svc_001"
        assert identity.zone_id == "z1"
