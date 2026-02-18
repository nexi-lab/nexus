"""LLM brick manifest and startup validation (Issue #1521).

Declares the LLM brick's metadata and provides verify_imports()
for validating required and optional modules at startup.
"""


import importlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMBrickManifest:
    """Brick manifest for the LLM module."""

    name: str = "llm"
    protocol: str = "LLMProviderProtocol"
    version: str = "1.0.0"
    config_schema: dict = field(
        default_factory=lambda: {
            "model": {"type": "str", "default": "claude-sonnet-4"},
            "temperature": {"type": "float", "default": 0.7},
            "max_output_tokens": {"type": "int", "default": 4096},
            "timeout": {"type": "float", "default": 120.0},
            "caching_prompt": {"type": "bool", "default": False},
        }
    )
    dependencies: list[str] = field(default_factory=list)


def verify_imports() -> dict[str, bool]:
    """Validate required and optional LLM imports at startup.

    Returns:
        Dict mapping module name to import success status.
    """
    results: dict[str, bool] = {}

    # Required modules
    for mod in [
        "litellm",
        "pydantic",
        "tenacity",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.error("Required LLM dependency missing: %s", mod)

    # Internal modules
    for mod in [
        "nexus.llm.config",
        "nexus.llm.provider",
        "nexus.llm.message",
        "nexus.llm.metrics",
        "nexus.llm.exceptions",
        "nexus.llm.cancellation",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.error("Required LLM module missing: %s", mod)

    return results
