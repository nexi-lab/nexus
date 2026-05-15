"""Shared grant input model and ReBAC tuple conversion (Issue #3130, #3128).

Provides a common ``GrantInput`` Pydantic model and a pure function
``grants_to_rebac_tuples()`` for converting admin-supplied path+role
grants into ReBAC relationship tuples.

Used by:
- ``AgentRegistrationService`` (Issue #3130)
- Scoped API key creation (Issue #3128, future)
"""

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

# Maximum number of grants per registration call.
MAX_REGISTRATION_GRANTS = 50

# Valid roles that map to ReBAC relations.
ROLE_TO_RELATION: dict[str, str] = {
    "editor": "direct_editor",
    "viewer": "direct_viewer",
}

# Path validation: reject traversal attempts and require leading slash.
_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")


class GrantInput(BaseModel):
    """A single path+role grant for agent registration.

    Attributes:
        path: Resource path or glob pattern (e.g. ``/workspace/*``).
            Must start with ``/`` and must not contain ``..`` traversal.
        role: Access level — ``"editor"`` (read/write) or ``"viewer"`` (read-only).
    """

    path: str = Field(
        ..., min_length=1, max_length=1024, description="Resource path or glob pattern"
    )
    role: str = Field(..., description="Access role: 'editor' or 'viewer'")


def validate_grant(grant: GrantInput) -> None:
    """Validate a single grant, raising ``ValueError`` on invalid input.

    Checks:
    - Role is one of ``editor``, ``viewer``.
    - Path starts with ``/``.
    - Path does not contain ``..`` traversal sequences.

    Raises:
        ValueError: With a descriptive message for the first violation found.
    """
    if grant.role not in ROLE_TO_RELATION:
        valid = ", ".join(sorted(ROLE_TO_RELATION))
        raise ValueError(
            f"Invalid role {grant.role!r} for path {grant.path!r}. Allowed roles: {valid}"
        )

    if not grant.path.startswith("/"):
        raise ValueError(f"Grant path must start with '/', got {grant.path!r}")

    if _PATH_TRAVERSAL_RE.search(grant.path):
        raise ValueError(f"Grant path must not contain '..' traversal: {grant.path!r}")


def grants_to_rebac_tuples(
    grants: list[GrantInput],
    agent_id: str,
    zone_id: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a list of GrantInput to ReBAC relationship tuple dicts.

    Pure function — no I/O, no side effects. Each grant becomes one
    tuple dict compatible with ``rebac_manager.rebac_write_batch()``.

    Args:
        grants: Validated list of GrantInput objects.
        agent_id: The agent receiving the grants.
        zone_id: Zone scope (defaults to ROOT_ZONE_ID).

    Returns:
        List of tuple dicts ready for ``rebac_write_batch()``.

    Raises:
        ValueError: If any grant fails validation or count exceeds
            ``MAX_REGISTRATION_GRANTS``.
    """
    if len(grants) > MAX_REGISTRATION_GRANTS:
        raise ValueError(
            f"Too many grants: {len(grants)} exceeds maximum of {MAX_REGISTRATION_GRANTS}"
        )

    tuples: list[dict[str, Any]] = []
    for grant in grants:
        validate_grant(grant)
        relation = ROLE_TO_RELATION[grant.role]
        tuples.append(
            {
                "subject": ("agent", agent_id),
                "relation": relation,
                "object": ("file", grant.path),
                "zone_id": zone_id or ROOT_ZONE_ID,
            }
        )

    return tuples
