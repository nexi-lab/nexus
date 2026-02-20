"""Backward-compat shim — canonical: nexus.rebac.utils.changelog.

Deprecated: import from nexus.rebac.utils.changelog instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.utils.changelog is deprecated. "
    "Import from nexus.rebac.utils.changelog instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.utils.changelog import (  # noqa: F401, E402
    CHANGELOG_INSERT_SQL,
    changelog_params,
    insert_changelog_entries_batch,
    insert_changelog_entry,
)

__all__ = [
    "CHANGELOG_INSERT_SQL",
    "changelog_params",
    "insert_changelog_entries_batch",
    "insert_changelog_entry",
]
