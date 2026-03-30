"""Kernel protocol compliance tests (Issue #2133).

Issue #2359: Kernel protocol compliance tests for EntityRegistry,
PermissionEnforcer, ReBACManager, and WorkspaceManager have been moved to
tests/unit/services/test_protocol_compliance.py (their protocols now live
in services/protocols/).

WiredServices was converted from a frozen dataclass to a plain dict
(see nexus.factory._wired). No dataclass validation test needed.
"""
