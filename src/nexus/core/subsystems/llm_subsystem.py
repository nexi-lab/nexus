"""LLM Subsystem — extracted from NexusFSLLMMixin.

Issue #1287: Extract NexusFS Domain Services from God Object (Phase B).

Wraps the existing ``LLMService`` with ``Subsystem`` lifecycle hooks
(health_check, cleanup). The ``LLMService`` owns the actual business
logic; this subsystem adds only the Subsystem ABC contract.

Constructor takes explicit deps — no ``self`` god-reference to NexusFS.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.subsystem import Subsystem

if TYPE_CHECKING:
    from nexus.services.llm_service import LLMService

logger = logging.getLogger(__name__)


class LLMSubsystem(Subsystem):
    """LLM-powered document reading subsystem.

    Delegates all business logic to ``LLMService``. Adds lifecycle
    management (health_check, cleanup) required by the Subsystem ABC.

    Args:
        llm_service: Existing LLMService instance with the 4 RPC methods.
    """

    def __init__(self, llm_service: LLMService) -> None:
        self._service = llm_service
        logger.info("[LLMSubsystem] Initialized")

    @property
    def service(self) -> LLMService:
        """Access the underlying LLMService."""
        return self._service

    def health_check(self) -> dict[str, Any]:
        """Return health status for the LLM subsystem.

        Always returns ``"ok"`` — the LLM subsystem has no persistent
        connections or background threads to monitor.
        """
        return {
            "status": "ok",
            "subsystem": "llm",
            "service_configured": self._service is not None,
        }

    def cleanup(self) -> None:
        """No-op — LLMService holds no persistent resources."""
