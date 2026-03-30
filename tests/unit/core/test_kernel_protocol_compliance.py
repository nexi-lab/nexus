"""Kernel protocol compliance tests (Issue #2133).

Issue #2359: Kernel protocol compliance tests for EntityRegistry,
PermissionEnforcer, ReBACManager, and WorkspaceManager have been moved to
tests/unit/services/test_protocol_compliance.py (their protocols now live
in services/protocols/).

WiredServices dataclass deleted — Tier 2b now returns plain dict.
"""
