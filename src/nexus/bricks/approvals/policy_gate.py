"""PolicyGate — sync facade hooks call to request a decision.

Hides the timeout/denied error mapping from callers: returns Decision.DENIED
on operator-deny, auto-deny TTL, or any non-DB error. GatewayClosed is the
only exception that propagates — callers map it to a hard fail (no false-allow).
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from nexus.bricks.approvals.errors import ApprovalDenied, ApprovalTimeout, GatewayClosed
from nexus.bricks.approvals.models import ApprovalKind, Decision
from nexus.bricks.approvals.service import ApprovalService, _has_real_session_id

logger = logging.getLogger(__name__)


def _new_request_id() -> str:
    return f"req_{secrets.token_hex(8)}"


class PolicyGate:
    """Sync facade for policy enforcement points.

    Used by MCP egress middleware (Task 18) and hub zone-access resolver
    (Task 19). Caller-facing semantics: returns APPROVED or DENIED. The
    DB-unreachable case raises GatewayClosed so the caller can choose to
    deny rather than silently allow.
    """

    def __init__(self, service: ApprovalService) -> None:
        self._service = service

    async def check(
        self,
        *,
        kind: ApprovalKind,
        subject: str,
        zone_id: str,
        token_id: str,
        session_id: str | None,
        agent_id: str | None,
        reason: str,
        metadata: dict[str, Any],
        timeout_override: float | None = None,
    ) -> Decision:
        # Session-scope cache hit: bypass the queue.
        #
        # F2 (#3790): only use the cache for real session_ids (non-None,
        # non-empty, non-fabricated). Empty strings and fabricated prefixes
        # (e.g. ``hub:...``) are excluded — see ``_has_real_session_id``.
        if _has_real_session_id(session_id):
            assert session_id is not None  # guaranteed by _has_real_session_id
            allow = await self._service.repository.session_allow_exists(
                session_id=session_id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
            )
            if allow:
                return Decision.APPROVED

        try:
            return await self._service.request_and_wait(
                request_id=_new_request_id(),
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                agent_id=agent_id,
                token_id=token_id,
                session_id=session_id,
                reason=reason,
                metadata=metadata,
                timeout_override=timeout_override,
            )
        except ApprovalDenied:
            return Decision.DENIED
        except ApprovalTimeout:
            return Decision.DENIED
        except GatewayClosed:
            raise
