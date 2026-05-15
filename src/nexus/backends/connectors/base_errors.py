"""Shared error definitions for connector validation framework (Issue #2086).

Contains error codes common across all connectors that use
``TraitBasedMixin`` and ``CheckpointMixin``.  Each connector's
``errors.py`` merges these with its own domain-specific codes::

    from nexus.backends.connectors.base_errors import TRAIT_ERRORS, CHECKPOINT_ERRORS
    ERROR_REGISTRY = {**TRAIT_ERRORS, **CHECKPOINT_ERRORS, **DOMAIN_ERRORS}
"""

from types import MappingProxyType

from nexus.backends.connectors.base import ErrorDef

# ---------------------------------------------------------------------------
# Trait validation errors (used by TraitBasedMixin.validate_traits)
# ---------------------------------------------------------------------------

TRAIT_ERRORS: MappingProxyType[str, ErrorDef] = MappingProxyType(
    {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require an 'agent_intent' comment explaining why this action is needed",
            readme_section="required-format",
            fix_example="# agent_intent: User requested to perform this operation",
        ),
        "AGENT_INTENT_TOO_SHORT": ErrorDef(
            message="agent_intent must be at least 10 characters to provide meaningful context",
            readme_section="required-format",
            fix_example="# agent_intent: User asked to perform this specific operation for this reason",
        ),
        "MISSING_CONFIRM": ErrorDef(
            message="This operation requires explicit confirmation with 'confirm: true'",
            readme_section="required-format",
            fix_example="confirm: true  # Add this to confirm the operation",
        ),
        "MISSING_USER_CONFIRMATION": ErrorDef(
            message="This operation requires explicit user confirmation before proceeding",
            readme_section="irreversible-operations",
            fix_example="# user_confirmed: true  # Only after explicit user approval",
        ),
    }
)

# ---------------------------------------------------------------------------
# Checkpoint errors (used by CheckpointMixin)
# ---------------------------------------------------------------------------

CHECKPOINT_ERRORS: MappingProxyType[str, ErrorDef] = MappingProxyType(
    {
        "CHECKPOINT_NOT_FOUND": ErrorDef(
            message="Checkpoint not found. It may have expired or been cleared",
            readme_section="rollback",
            fix_example="# Checkpoints expire after the operation completes successfully",
        ),
        "ROLLBACK_NOT_POSSIBLE": ErrorDef(
            message="Cannot rollback this operation",
            readme_section="rollback",
            fix_example="# Some operations (like notifications sent) cannot be undone",
        ),
    }
)
