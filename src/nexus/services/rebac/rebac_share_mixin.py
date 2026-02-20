"""Backward-compat shim — canonical: nexus.rebac.share_mixin.

Issue #1891: ReBACShareMixin moved to rebac/ brick.
"""

from nexus.rebac.share_mixin import ReBACShareMixin  # noqa: F401

__all__ = ["ReBACShareMixin"]
