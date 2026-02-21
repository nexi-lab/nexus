"""Describable protocol for recursive wrapper chain introspection (#1449).

Cross-tier contract for brick composition introspection.

Any brick that wraps another brick implementing the same Protocol MUST
implement ``describe()`` to return a human-readable chain description
for debugging.  Leaf bricks (non-wrappers) return their ``name``.

Example chain output::

    storage.describe()  # "cache → logging → s3"

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Brick Composition Patterns
    - Recursive Wrapping Rule #3: every wrapper MUST implement describe()
    - Issue #2359: Moved from core/protocols/ to contracts/ (cross-tier)
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Describable(Protocol):
    """Protocol for components that can describe their composition chain.

    Wrappers return ``"<layer> → {inner.describe()}"``.
    Leaf implementations return their ``name``.
    """

    def describe(self) -> str: ...
