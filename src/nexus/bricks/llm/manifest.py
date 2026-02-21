"""LLM brick manifest (Issue #1521).

Extends :class:`~nexus.contracts.brick_manifest.BrickManifest` with
LLM-specific configuration and module declarations.
"""

from dataclasses import dataclass, field

from nexus.contracts.brick_manifest import BrickManifest


@dataclass(frozen=True)
class LLMBrickManifest(BrickManifest):
    """Brick manifest for the LLM module."""

    name: str = "llm"
    protocol: str = "LLMProviderProtocol"
    config_schema: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "model": {"type": "str", "default": "claude-sonnet-4"},
            "temperature": {"type": "float", "default": 0.7},
            "max_output_tokens": {"type": "int", "default": 4096},
            "timeout": {"type": "float", "default": 120.0},
            "caching_prompt": {"type": "bool", "default": False},
        }
    )
    required_modules: tuple[str, ...] = (
        "litellm",
        "pydantic",
        "tenacity",
        "nexus.bricks.llm.config",
        "nexus.bricks.llm.provider",
        "nexus.bricks.llm.message",
        "nexus.bricks.llm.metrics",
        "nexus.bricks.llm.exceptions",
        "nexus.bricks.llm.cancellation",
    )


def verify_imports() -> dict[str, bool]:
    """Convenience wrapper — instantiates manifest and verifies imports."""
    return LLMBrickManifest().verify_imports()
