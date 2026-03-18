"""Re-export shim — canonical location is nexus.contracts.grant_helpers (Issue #3130).

Kept for backward compatibility with any existing bricks-layer imports.
"""

from nexus.contracts.grant_helpers import (  # noqa: F401
    MAX_REGISTRATION_GRANTS,
    GrantInput,
    grants_to_rebac_tuples,
    validate_grant,
)
