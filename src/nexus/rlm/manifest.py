"""RLM brick manifest — declares the brick's identity and config schema.

Extends :class:`~nexus.contracts.brick_manifest.BrickManifest` with
RLM-specific configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nexus.contracts.brick_manifest import BrickManifest


@dataclass(frozen=True)
class RLMBrickManifest(BrickManifest):
    """Manifest for the RLM inference brick."""

    name: str = "rlm"
    protocol: str = "RLMInferenceProtocol"
    description: str = (
        "Recursive Language Model inference for unbounded context processing. "
        "Enables agents to process near-infinite context (10M+ tokens) by "
        "recursively decomposing and reasoning over data stored in Nexus VFS."
    )
    config_schema: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "model": {"type": "str", "default": "claude-sonnet-4-20250514"},
            "sub_model": {"type": "str", "default": None},
            "max_iterations": {"type": "int", "default": 15, "min": 1, "max": 50},
            "max_duration_seconds": {"type": "int", "default": 120, "min": 10, "max": 600},
            "max_total_tokens": {"type": "int", "default": 100_000, "min": 1_000, "max": 1_000_000},
            "max_concurrent": {"type": "int", "default": 8, "min": 1, "max": 32},
        }
    )
