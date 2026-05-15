"""Regression tests for UnifiedAuthService — Phase 4 dual-read removal (#3741)."""

from __future__ import annotations


def test_unified_service_does_not_wrap_profile_store_in_dual_read():
    """Phase 4 — profile store is authoritative; dual-read is gone from the read path."""
    import inspect

    from nexus.bricks.auth import unified_service

    assert "DualReadAuthProfileStore" not in inspect.getsource(unified_service), (
        "DualReadAuthProfileStore was Phase 1 only — expected to be removed after finalize (#3741)"
    )
