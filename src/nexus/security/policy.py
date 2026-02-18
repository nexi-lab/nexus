"""Configurable injection policy for prompt security (Issue #1756).

Defines severity-based actions (log, block, escalate) that can be
configured per-deployment without code changes.

Usage::

    from nexus.security.policy import InjectionAction, InjectionPolicyConfig

    # Block high-severity, log everything else
    policy = InjectionPolicyConfig(
        high_severity_action=InjectionAction.BLOCK,
    )
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class InjectionAction(StrEnum):
    """Action to take when an injection pattern is detected."""

    LOG = "log"  # Log only (default)
    BLOCK = "block"  # Reject request
    ESCALATE = "escalate"  # Log + trigger alert callback


@dataclass(frozen=True)
class InjectionPolicyConfig:
    """Configuration for injection detection enforcement.

    Each severity level can be configured independently. The default
    is LOG-only for all severities (non-breaking deployment).

    Attributes:
        default_action: Fallback action for unknown severities.
        high_severity_action: Action for high-severity patterns.
        medium_severity_action: Action for medium-severity patterns.
        low_severity_action: Action for low-severity patterns.
        escalation_callback: Optional callback for ESCALATE actions.
            Receives (text, detections) where detections is a list
            of (pattern_name, severity) tuples.
    """

    default_action: InjectionAction = InjectionAction.LOG
    high_severity_action: InjectionAction = InjectionAction.LOG
    medium_severity_action: InjectionAction = InjectionAction.LOG
    low_severity_action: InjectionAction = InjectionAction.LOG
    escalation_callback: Callable[[str, list[tuple[str, str]]], None] | None = field(
        default=None, compare=False, hash=False
    )
