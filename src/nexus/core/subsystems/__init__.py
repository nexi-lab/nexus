"""Extracted NexusFS subsystems.

Issue #1287: Extract NexusFS Domain Services from God Object.

Each subsystem is a standalone class extending ``Subsystem`` ABC,
with explicit constructor dependencies (no self god-reference).
"""

from nexus.core.subsystems.llm_subsystem import LLMSubsystem

__all__ = ["LLMSubsystem"]
