"""Access-context typing for secrets / password-vault read endpoints.

A caller tags every credential read with an ``access_context`` so the
audit log can later distinguish an admin CLI lookup from an agent
auto-login from a human-approved reveal. The value is observability only
— it is not enforced for access control.

Lives in ``nexus.contracts`` (not ``services/password_vault``) so both
the ``secrets`` brick and the ``password_vault`` service wrapper can
import it without creating a bricks→services dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

AccessContext = Literal[
    "admin_cli",
    "auto_login",
    "auto_rotate",
    "reveal_approved",
    "agent_direct",
]

DEFAULT_ACCESS_CONTEXT: AccessContext = "admin_cli"

ACCESS_CONTEXT_VALUES: frozenset[str] = frozenset(get_args(AccessContext))


@dataclass(frozen=True, slots=True)
class AccessAuditContext:
    """Audit-only context carried with every credential-read call.

    Fields are informational — no server-side enforcement. They land in
    the ``details`` JSON of the ``secrets_audit_log`` row so downstream
    queries can slice reads by caller identity / agent session.

    Attributes:
        access_context: Who/why is reading. Defaults to ``admin_cli``.
        client_id: Free-form client identifier (e.g. ``"sudowork"``).
        agent_session: Free-form agent session identifier. Pairs with
            ``client_id`` to let operators reconstruct "what did this
            agent session read?"
    """

    access_context: AccessContext = DEFAULT_ACCESS_CONTEXT
    client_id: str | None = None
    agent_session: str | None = None

    def to_audit_details(self) -> dict[str, Any]:
        """Return non-None fields for merging into audit ``details``."""
        d: dict[str, Any] = {"access_context": self.access_context}
        if self.client_id is not None:
            d["client_id"] = self.client_id
        if self.agent_session is not None:
            d["agent_session"] = self.agent_session
        return d
