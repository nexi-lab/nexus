"""Default no-op permission enforcer (kernel primitive).

The kernel constructs ``AllowAllEnforcer`` at init as the default
permission policy — like Linux DAC with ``CAP_DAC_OVERRIDE`` (root).
Factory overrides with ``ReBACPermissionEnforcer`` at link-time if
permission enforcement is enabled.

Issue #1815.
"""

from __future__ import annotations

from typing import Any


class AllowAllEnforcer:
    """Default no-op permission enforcer (like Linux DAC with root).

    Kernel constructs this as default.  Factory overrides with
    ``ReBACPermissionEnforcer`` at link-time.

    All permission checks return True (allow-all).
    """

    def check_owner(self, metadata: Any, context: Any) -> bool:  # noqa: ARG002
        """AllowAll: owner check always passes (like root)."""
        return True

    def check(self, path: str, permission: Any, context: Any) -> bool:  # noqa: ARG002
        """Always grants permission."""
        return True

    def filter_list(self, paths: list[str], context: Any) -> list[str]:  # noqa: ARG002
        """Returns all paths unfiltered."""
        return paths

    def has_accessible_descendants(self, prefix: str, context: Any) -> bool:  # noqa: ARG002
        """Always returns True (all descendants accessible)."""
        return True

    def has_accessible_descendants_batch(
        self,
        prefixes: list[str],
        context: Any,  # noqa: ARG002
    ) -> dict[str, bool]:
        """Returns True for all prefixes."""
        return dict.fromkeys(prefixes, True)

    def invalidate_cache(self, **kwargs: Any) -> None:
        """No-op (no cache to invalidate)."""
