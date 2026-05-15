"""Factory helpers — _safe_create, _make_gate."""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile gating helper (Issue #2193: DRY for _on() closure)
# ---------------------------------------------------------------------------


def _make_gate(svc_on: Callable[[str], bool] | None) -> Callable[[str], bool]:
    """Create a profile gate closure.

    Replaces the repeated ``_on()`` inner function pattern across tier modules.

    Args:
        svc_on: Callable ``(name: str) -> bool`` for profile-based gating.
            When None, returns a gate that enables everything.

    Returns:
        A ``(name: str) -> bool`` callable for gating service creation.
    """
    if svc_on is None:
        return lambda _name: True
    return svc_on


def _safe_create(
    name: str,
    factory_fn: Callable[[], Any],
    svc_on: Callable[[str], bool],
    tier: str = "BRICK",
    severity: str = "debug",
) -> Any:
    """Create a service with profile gating + error handling.

    Severity levels (Issue #2193):
        ``"debug"``   — Brick-tier default.  Log at DEBUG on failure, return None.
        ``"warning"`` — System-tier degradable.  Log at WARNING on failure, return None.
        ``"critical"``— System-tier critical.  Log at CRITICAL and raise ``BootError``.

    Returns the created service, or None if gated or on non-critical failure.
    """
    if not svc_on(name):
        logger.debug("[BOOT:%s] %s disabled by profile", tier, name)
        return None
    try:
        result = factory_fn()
        logger.debug("[BOOT:%s] %s created", tier, name)
        return result
    except Exception as exc:
        if severity == "critical":
            from nexus.contracts.exceptions import BootError

            logger.critical("[BOOT:%s] %s FATAL: %s", tier, name, exc)
            raise BootError(f"{name}: {exc}", tier=tier) from exc
        getattr(logger, severity)("[BOOT:%s] %s unavailable: %s", tier, name, exc)
        return None
